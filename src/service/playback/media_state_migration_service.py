"""Move user playback state when a JAV media version is deleted."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    MomentCollectionItem,
)
from src.service.catalog.image_cleanup_service import ImageCleanupService

_RESOLUTION_PATTERN = re.compile(r"^(\d+)x(\d+)$")


@dataclass(slots=True)
class MediaStateMigrationResult:
    target_media_id: int
    progress_migrated: bool = False
    points_migrated: int = 0
    points_merged: int = 0
    points_skipped_out_of_range: int = 0
    obsolete_image_paths: set[str] = field(default_factory=set)


class MediaStateMigrationService:
    """Choose a surviving media version and move progress/points onto it."""

    @classmethod
    def resolve_target_media(
        cls,
        source: Media,
        *,
        excluded_media_ids: frozenset[int] = frozenset(),
    ) -> Media | None:
        if not source.movie_number:
            return None

        query = (
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary)
            .where(
                Media.movie == source.movie_number,
                Media.valid == True,
                Media.id != source.id,
            )
        )
        if excluded_media_ids:
            query = query.where(Media.id.not_in(list(excluded_media_ids)))

        candidates = list(query)
        if not candidates:
            return None
        return max(candidates, key=cls._target_sort_key)

    @classmethod
    def migrate(cls, source: Media, target: Media) -> MediaStateMigrationResult:
        if (
            source.id == target.id
            or source.movie_number != target.movie_number
            or not target.valid
        ):
            raise ValueError("invalid media state migration target")

        result = MediaStateMigrationResult(target_media_id=target.id)
        result.progress_migrated = cls._migrate_progress(source, target)
        (
            result.points_migrated,
            result.points_merged,
            result.points_skipped_out_of_range,
            result.obsolete_image_paths,
        ) = cls._migrate_points(source, target)
        return result

    @classmethod
    def _target_sort_key(cls, media: Media) -> tuple[int, int, int, int, float, int]:
        return (
            cls._effective_bit_rate(media),
            cls._resolution_level(media.resolution),
            media.file_size_bytes or 0,
            cls._library_priority(media),
            media.created_at.timestamp() if media.created_at else 0.0,
            media.id,
        )

    @classmethod
    def _effective_bit_rate(cls, media: Media) -> int:
        raw_bit_rate: Any = None
        video_info = media.video_info
        if isinstance(video_info, Mapping):
            video = video_info.get("video")
            if isinstance(video, Mapping):
                raw_bit_rate = video.get("bit_rate")
        try:
            measured = int(float(raw_bit_rate))
        except (TypeError, ValueError, OverflowError):
            measured = 0
        if measured > 0:
            return measured

        duration_seconds = media.duration_seconds or 0
        file_size_bytes = media.file_size_bytes or 0
        if duration_seconds > 0 and file_size_bytes > 0:
            return round(file_size_bytes * 8 / duration_seconds)
        return 0

    @staticmethod
    def _resolution_level(resolution: str | None) -> int:
        if not resolution:
            return 0
        match = _RESOLUTION_PATTERN.fullmatch(resolution.strip())
        if match is None:
            return 0
        width, height = int(match.group(1)), int(match.group(2))
        if width <= 0 or height <= 0:
            return 0
        if width >= 7680:
            return 7
        if width >= 3840:
            return 6
        if height >= 1440:
            return 5
        if height >= 1080:
            return 4
        if height >= 720:
            return 3
        if height >= 480:
            return 2
        if height >= 360:
            return 1
        return 0

    @staticmethod
    def _library_priority(media: Media) -> int:
        return 1 if media.library.provider_key == "local" else 0

    @classmethod
    def _migrate_progress(cls, source: Media, target: Media) -> bool:
        source_progress = MediaProgress.get_or_none(MediaProgress.media == source.id)
        if source_progress is None or source_progress.position_seconds <= 0:
            return False

        target_progress = MediaProgress.get_or_none(MediaProgress.media == target.id)
        if target_progress is not None:
            source_watched_at = source_progress.last_watched_at
            target_watched_at = target_progress.last_watched_at
            if source_watched_at is None or (
                target_watched_at is not None
                and source_watched_at <= target_watched_at
            ):
                return False

        position_seconds = cls._map_progress_position(
            source_progress.position_seconds,
            source.duration_seconds,
            target.duration_seconds,
        )
        if target_progress is None:
            MediaProgress.create(
                media=target,
                position_seconds=position_seconds,
                last_watched_at=source_progress.last_watched_at,
                created_at=source_progress.created_at,
                updated_at=source_progress.updated_at,
            )
        else:
            target_progress.position_seconds = position_seconds
            target_progress.last_watched_at = source_progress.last_watched_at
            target_progress.save(
                only=[
                    MediaProgress.position_seconds,
                    MediaProgress.last_watched_at,
                ]
            )
        return True

    @staticmethod
    def _map_progress_position(
        position_seconds: int,
        source_duration_seconds: int,
        target_duration_seconds: int,
    ) -> int:
        position_seconds = max(0, int(position_seconds))
        if (
            source_duration_seconds <= 0
            or target_duration_seconds <= 0
            or abs(source_duration_seconds - target_duration_seconds) <= 1
        ):
            mapped = position_seconds
        else:
            mapped = round(
                position_seconds * target_duration_seconds / source_duration_seconds
            )
        if target_duration_seconds > 0:
            mapped = min(mapped, target_duration_seconds)
        return max(0, mapped)

    @classmethod
    def _migrate_points(
        cls,
        source: Media,
        target: Media,
    ) -> tuple[int, int, int, set[str]]:
        target_thumbnails = {
            thumbnail.offset: thumbnail
            for thumbnail in MediaThumbnail.select().where(
                MediaThumbnail.media == target.id
            )
        }
        target_points: dict[int, MediaPoint] = {}
        for point in (
            MediaPoint.select()
            .where(MediaPoint.media == target.id)
            .order_by(MediaPoint.id)
        ):
            target_points.setdefault(point.offset_seconds, point)
        source_points = list(
            MediaPoint.select(MediaPoint, Image)
            .join(Image)
            .where(MediaPoint.media == source.id)
            .order_by(MediaPoint.id)
        )

        migrated = 0
        merged = 0
        skipped = 0
        obsolete_image_paths: set[str] = set()
        for point in source_points:
            if (
                target.duration_seconds > 0
                and point.offset_seconds > target.duration_seconds
            ):
                skipped += 1
                continue

            duplicate = target_points.get(point.offset_seconds)
            if duplicate is not None:
                cls._merge_point_collections(point, duplicate)
                image = point.image
                point.delete_instance()
                obsolete_image_paths |= (
                    ImageCleanupService.delete_image_record_if_unused(image)
                )
                merged += 1
                continue

            point.media = target
            point.movie_number = target.movie_number
            point.video_item_id = None
            point.thumbnail = target_thumbnails.get(point.offset_seconds)
            point.save(
                only=[
                    MediaPoint.media,
                    MediaPoint.movie_number,
                    MediaPoint.video_item_id,
                    MediaPoint.thumbnail,
                ]
            )
            target_points[point.offset_seconds] = point
            migrated += 1

        return migrated, merged, skipped, obsolete_image_paths

    @staticmethod
    def _merge_point_collections(
        source_point: MediaPoint,
        target_point: MediaPoint,
    ) -> None:
        for item in MomentCollectionItem.select().where(
            MomentCollectionItem.point == source_point.id
        ):
            existing = MomentCollectionItem.get_or_none(
                (MomentCollectionItem.collection == item.collection)
                & (MomentCollectionItem.point == target_point.id)
            )
            if existing is None:
                item.point = target_point
                item.save(only=[MomentCollectionItem.point])
            else:
                item.delete_instance()
