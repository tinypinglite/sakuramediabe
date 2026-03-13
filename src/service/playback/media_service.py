from datetime import datetime
from pathlib import Path
from typing import Sequence

from loguru import logger

from src.api.exception.errors import ApiError
from src.model import Media, MediaPoint, MediaProgress, Movie
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    MediaPointListItemResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaThumbnailResource,
)
from src.service.collections import PlaylistService
from src.service.discovery import get_lancedb_thumbnail_store
from src.service.playback.media_thumbnail_service import MediaThumbnailService


class MediaService:
    MEDIA_POINT_SORT_FIELDS = {
        "created_at:desc": [MediaPoint.created_at.desc(), MediaPoint.id.desc()],
        "created_at:asc": [MediaPoint.created_at.asc(), MediaPoint.id.asc()],
    }

    @staticmethod
    def _current_time() -> datetime:
        return datetime.utcnow()

    @staticmethod
    def _require_media(media_id: int) -> Media:
        media = Media.get_or_none(Media.id == media_id)
        if media is None:
            raise ApiError(
                404,
                "media_not_found",
                "Media not found",
                {"media_id": media_id},
            )
        return media

    @classmethod
    def _resolve_media_point_sort(cls, value: str | None) -> Sequence:
        if value is None:
            return cls.MEDIA_POINT_SORT_FIELDS["created_at:desc"]
        normalized = value.strip().lower()
        if not normalized:
            return cls.MEDIA_POINT_SORT_FIELDS["created_at:desc"]
        if normalized not in cls.MEDIA_POINT_SORT_FIELDS:
            raise ApiError(
                422,
                "invalid_media_point_filter",
                "Invalid sort expression",
                {"sort": value},
            )
        return cls.MEDIA_POINT_SORT_FIELDS[normalized]

    @staticmethod
    def _validate_media_point_page(page: int, page_size: int) -> None:
        if page <= 0:
            raise ApiError(
                422,
                "invalid_media_point_filter",
                "page must be greater than 0",
                {"page": page},
            )
        if page_size <= 0 or page_size > 100:
            raise ApiError(
                422,
                "invalid_media_point_filter",
                "page_size must be between 1 and 100",
                {"page_size": page_size},
            )

    @staticmethod
    def _delete_local_media_file(media: Media) -> None:
        try:
            Path(media.path).unlink()
        except FileNotFoundError:
            return

    @classmethod
    def list_media_points(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
    ) -> PageResponse[MediaPointListItemResource]:
        cls._validate_media_point_page(page, page_size)
        start = (page - 1) * page_size
        order_by = cls._resolve_media_point_sort(sort)
        total = MediaPoint.select().count()
        points = list(
            MediaPoint.select(MediaPoint, Media, Movie)
            .join(Media)
            .switch(Media)
            .join(Movie, on=(Media.movie == Movie.movie_number))
            .order_by(*order_by)
            .offset(start)
            .limit(page_size)
        )
        items = [
            MediaPointListItemResource(
                point_id=point.id,
                media_id=point.media_id,
                movie_number=point.media.movie.movie_number,
                offset_seconds=point.offset_seconds,
                created_at=point.created_at,
            )
            for point in points
        ]
        return PageResponse[MediaPointListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def update_progress(
        cls,
        media_id: int,
        payload: MediaProgressUpdateRequest,
    ) -> MediaProgressResource:
        media = cls._require_media(media_id)
        watched_at = cls._current_time()
        progress = MediaProgress.get_or_none(MediaProgress.media == media)
        if progress is None:
            progress = MediaProgress.create(
                media=media,
                position_seconds=payload.position_seconds,
                last_watched_at=watched_at,
                created_at=watched_at,
                updated_at=watched_at,
            )
        else:
            progress.position_seconds = payload.position_seconds
            progress.last_watched_at = watched_at
            progress.updated_at = watched_at
            progress.save()

        PlaylistService.touch_recently_played(media.movie)
        return MediaProgressResource(
            media_id=media.id,
            last_position_seconds=progress.position_seconds,
            last_watched_at=progress.last_watched_at,
        )

    @classmethod
    def delete_media(cls, media_id: int) -> None:
        media = cls._require_media(media_id)
        cls._delete_local_media_file(media)
        deleted_at = cls._current_time()
        with get_database().atomic():
            MediaProgress.delete().where(MediaProgress.media == media).execute()
            MediaPoint.delete().where(MediaPoint.media == media).execute()
            media.valid = False
            media.updated_at = deleted_at
            media.save(only=[Media.valid, Media.updated_at])
        try:
            get_lancedb_thumbnail_store().delete_by_media_id(media.id)
        except Exception as exc:
            logger.warning("Delete media vectors failed media_id={} detail={}", media.id, exc)

    @classmethod
    def list_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        cls._require_media(media_id)
        return MediaThumbnailService.list_media_thumbnails(media_id)
