from __future__ import annotations

from typing import Any

from loguru import logger
from peewee import fn

from src.common.media_formats import normalize_media_resolution
from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import media_handle_for


class MediaVideoInfoBackfillService:
    """补齐时长和分辨率，并用更完整的探测结果更新媒体技术信息。"""

    TASK_KEY = "media_video_info_backfill"

    @staticmethod
    def _missing_duration_condition():
        return Media.duration_seconds <= 0

    @staticmethod
    def _missing_resolution_condition():
        return Media.resolution.is_null(True) | (fn.TRIM(Media.resolution) == "")

    @classmethod
    def _missing_info_condition(cls):
        return (
            Media.video_info.is_null(True)
            | cls._missing_duration_condition()
            | cls._missing_resolution_condition()
        )

    @classmethod
    def _info_fields(cls, value: Any) -> set[tuple]:
        if isinstance(value, dict):
            return {
                (key, *path)
                for key, item in value.items()
                for path in cls._info_fields(item)
            }
        if isinstance(value, list):
            return {
                (index, *path)
                for index, item in enumerate(value)
                for path in cls._info_fields(item)
            }
        # 空值和估算标记不代表额外的技术信息。
        if isinstance(value, str) and value.strip():
            return {()}
        if type(value) in (int, float) and value > 0:
            return {()}
        return set()

    @classmethod
    def _save_missing_info(cls, media: Media, info: dict) -> bool:
        container = info.get("container")
        video = info.get("video")
        duration = (
            container.get("duration_seconds") if isinstance(container, dict) else None
        )
        resolution = (
            normalize_media_resolution(f"{video.get('width')}x{video.get('height')}")
            if isinstance(video, dict)
            else None
        )
        values = []
        if media.video_info is None:
            values.append((Media.video_info, Media.video_info.is_null(True), info))
        elif cls._info_fields(info) > cls._info_fields(media.video_info):
            values.append((Media.video_info, Media.video_info == media.video_info, info))
        if type(duration) is int and duration > 0:
            values.append(
                (Media.duration_seconds, cls._missing_duration_condition(), duration)
            )
        if resolution is not None:
            values.append(
                (Media.resolution, cls._missing_resolution_condition(), resolution)
            )
        updated = False
        # 技术信息须与探测前一致，其他字段须仍缺失，避免覆盖并发写入。
        with Media._meta.database.atomic():
            for field, condition, value in values:
                count = (
                    Media.update({field: value, Media.updated_at: utc_now_for_db()})
                    .where(Media.id == media.id, Media.valid == True, condition)
                    .execute()
                )
                updated = updated or count > 0
        return updated

    @classmethod
    def _candidate_ids(cls) -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                Media.select(Media.id)
                .where(Media.valid == True, cls._missing_info_condition())
                .order_by(Media.id)
                .tuples()
            )
        ]

    @classmethod
    def backfill_missing_video_infos(cls, *, reporter) -> dict[str, Any]:
        media_ids = cls._candidate_ids()
        stats: dict[str, Any] = {
            "missing_media": len(media_ids),
            "updated_media": 0,
            "failed_media": 0,
            "skipped_media": 0,
            "incomplete_media": 0,
        }
        storage_by_library: dict[int, Any] = {}
        logger.info("Media video info backfill started missing_media={}", len(media_ids))
        step = max(len(media_ids) // 20, 1)

        def emit_progress(completed: int) -> None:
            reporter.emit(
                current=completed,
                total=len(media_ids),
                text=(
                    f"媒体信息回填 · 已完成 {completed}/{len(media_ids)}"
                    f" · 已更新 {stats['updated_media']}"
                    f" · 跳过 {stats['skipped_media']}"
                    f" · 失败 {stats['failed_media']}"
                    f" · 已更新但仍缺失 {stats['incomplete_media']}"
                ),
                summary_patch=stats,
            )

        emit_progress(0)
        for completed, media_id in enumerate(media_ids, start=1):
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Media video info backfill progress completed={}/{} updated={} skipped={} failed={}",
                    completed,
                    len(media_ids),
                    stats["updated_media"],
                    stats["skipped_media"],
                    stats["failed_media"],
                )
            reporter.emit(
                current=completed - 1,
                total=len(media_ids),
                text=(
                    f"媒体信息回填 · 正在探测 {completed}/{len(media_ids)}"
                    f" · 已更新 {stats['updated_media']} · 失败 {stats['failed_media']}"
                    f" · 跳过 {stats['skipped_media']}"
                ),
                summary_patch=stats,
            )
            try:
                with media_operation_lock(MEDIA_LOCK, media_id):
                    media = (
                        Media.select(Media, MediaLibrary)
                        .join(MediaLibrary)
                        .where(
                            Media.id == media_id,
                            Media.valid == True,
                            cls._missing_info_condition(),
                        )
                        .get_or_none()
                    )
                    if media is None:
                        stats["skipped_media"] += 1
                        emit_progress(completed)
                        continue

                    try:
                        media_handle = media_handle_for(media)
                        storage = storage_by_library.get(media.library_id)
                        if storage is None:
                            storage = MEDIA_PROVIDER_REGISTRY.storage_for(
                                media_handle.library
                            )
                            storage_by_library[media.library_id] = storage
                        probe_video_info = getattr(storage, "probe_video_info", None)
                        if not callable(probe_video_info):
                            stats["skipped_media"] += 1
                            continue
                        provider_video_info = probe_video_info(media=media_handle)
                        if (
                            not isinstance(provider_video_info, dict)
                            or not provider_video_info
                        ):
                            raise ValueError(
                                "provider returned no valid media video info"
                            )
                        updated = cls._save_missing_info(media, provider_video_info)
                        incomplete = (
                            Media.select()
                            .where(
                                Media.id == media.id,
                                Media.valid == True,
                                cls._missing_info_condition(),
                            )
                            .exists()
                        )
                        if updated:
                            stats["updated_media"] += 1
                            if incomplete:
                                stats["incomplete_media"] += 1
                        elif incomplete:
                            raise ValueError(
                                "provider returned no usable missing media info"
                            )
                        else:
                            stats["skipped_media"] += 1
                    except Exception as exc:
                        stats["failed_media"] += 1
                        logger.warning(
                            "Media video_info backfill failed media_id={} library_id={} detail={}",
                            media.id,
                            media.library_id,
                            exc,
                        )
                    finally:
                        emit_progress(completed)

            except MediaOperationBusy:
                stats["skipped_media"] += 1
                emit_progress(completed)

        return stats
