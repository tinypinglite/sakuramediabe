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


class MediaResolutionBackfillService:
    """Fill missing dimensions through the owning storage provider."""

    TASK_KEY = "media_resolution_backfill"

    @staticmethod
    def _missing_resolution_condition():
        return Media.resolution.is_null(True) | (fn.TRIM(Media.resolution) == "")

    @classmethod
    def _candidate_ids(cls) -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                Media.select(Media.id)
                .where(Media.valid == True, cls._missing_resolution_condition())
                .order_by(Media.id)
                .tuples()
            )
        ]

    @classmethod
    def backfill_missing_resolutions(cls, *, reporter) -> dict[str, Any]:
        media_ids = cls._candidate_ids()
        stats: dict[str, Any] = {
            "missing_media": len(media_ids),
            "updated_media": 0,
            "failed_media": 0,
            "skipped_media": 0,
        }
        storage_by_library: dict[int, Any] = {}

        for completed, media_id in enumerate(media_ids, start=1):
            try:
                with media_operation_lock(MEDIA_LOCK, media_id):
                    media = (
                        Media.select(Media, MediaLibrary)
                        .join(MediaLibrary)
                        .where(
                            Media.id == media_id,
                            Media.valid == True,
                            cls._missing_resolution_condition(),
                        )
                        .get_or_none()
                    )
                    if media is None:
                        stats["skipped_media"] += 1
                        reporter.emit(
                            current=completed, total=len(media_ids), summary_patch=stats
                        )
                        continue

                    try:
                        media_handle = media_handle_for(media)
                        storage = storage_by_library.get(media.library_id)
                        if storage is None:
                            storage = MEDIA_PROVIDER_REGISTRY.storage_for(media_handle.library)
                            storage_by_library[media.library_id] = storage
                        probe_resolution = getattr(storage, "probe_resolution", None)
                        if not callable(probe_resolution):
                            stats["skipped_media"] += 1
                            continue
                        provider_resolution = probe_resolution(media=media_handle)
                        if provider_resolution is None:
                            stats["skipped_media"] += 1
                            continue
                        resolution = normalize_media_resolution(provider_resolution)
                        if resolution is None:
                            raise ValueError("provider returned an invalid media resolution")
                        updated = (
                            Media.update(
                                resolution=resolution,
                                updated_at=utc_now_for_db(),
                            )
                            .where(
                                Media.id == media.id,
                                Media.valid == True,
                                cls._missing_resolution_condition(),
                            )
                            .execute()
                        )
                        if updated != 1:
                            stats["skipped_media"] += 1
                        else:
                            stats["updated_media"] += 1
                    except Exception as exc:
                        stats["failed_media"] += 1
                        logger.warning(
                            "Media resolution backfill failed media_id={} library_id={} detail={}",
                            media.id,
                            media.library_id,
                            exc,
                        )
                    finally:
                        reporter.emit(
                            current=completed, total=len(media_ids), summary_patch=stats
                        )

            except MediaOperationBusy:
                stats["skipped_media"] += 1
                reporter.emit(current=completed, total=len(media_ids), summary_patch=stats)

        return stats
