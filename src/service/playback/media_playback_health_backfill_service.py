from __future__ import annotations

from typing import Any

from loguru import logger
from peewee import fn

from src.i18n import translate
from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback.media_playback_health_service import (
    STATUS_INVALID,
    STATUS_OK,
    STATUS_UNKNOWN,
    MediaPlaybackHealthService,
)
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import media_handle_for


class MediaPlaybackHealthBackfillService:
    TASK_KEY = "media_playback_health_backfill"

    @staticmethod
    def _candidate_condition():
        return (Media.valid == True) & (
            Media.playback_health.is_null(True)
            | (
                fn.jsonb_extract_path_text(Media.playback_health, "status")
                == STATUS_UNKNOWN
            )
        )

    @classmethod
    def _candidate_ids(cls) -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                Media.select(Media.id)
                .where(cls._candidate_condition())
                .order_by(Media.id)
                .tuples()
            )
        ]

    @classmethod
    def backfill_pending_health(cls, *, reporter) -> dict[str, Any]:
        media_ids = cls._candidate_ids()
        stats: dict[str, Any] = {
            "candidate_media": len(media_ids),
            "updated_media": 0,
            "invalid_media": 0,
            "ok_media": 0,
            "unknown_media": 0,
            "skipped_media": 0,
            "failed_media": 0,
        }
        storage_by_library: dict[int, Any] = {}
        logger.info(
            "Media playback health backfill started candidate_media={}",
            len(media_ids),
        )
        step = max(len(media_ids) // 20, 1)
        reporter.emit(
            current=0,
            total=len(media_ids),
            text=translate(
                "tasks.media_playback_health_backfill.progress.completed",
                current=0,
                total=len(media_ids),
                updated=0,
                invalid=0,
                unknown=0,
                failed=0,
            ),
            summary_patch=stats,
        )

        def emit_progress(completed: int) -> None:
            reporter.emit(
                current=completed,
                total=len(media_ids),
                text=translate(
                    "tasks.media_playback_health_backfill.progress.completed",
                    current=completed,
                    total=len(media_ids),
                    updated=stats["updated_media"],
                    invalid=stats["invalid_media"],
                    unknown=stats["unknown_media"],
                    failed=stats["failed_media"],
                ),
                summary_patch=stats,
            )

        for completed, media_id in enumerate(media_ids, start=1):
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Media playback health backfill progress completed={}/{} "
                    "updated={} invalid={} unknown={} failed={}",
                    completed,
                    len(media_ids),
                    stats["updated_media"],
                    stats["invalid_media"],
                    stats["unknown_media"],
                    stats["failed_media"],
                )
            try:
                with media_operation_lock(MEDIA_LOCK, media_id):
                    media = (
                        Media.select(Media, MediaLibrary)
                        .join(MediaLibrary)
                        .where(
                            Media.id == media_id,
                            cls._candidate_condition(),
                        )
                        .get_or_none()
                    )
                    if media is None:
                        stats["skipped_media"] += 1
                        emit_progress(completed)
                        continue

                    handle = media_handle_for(media)
                    storage = storage_by_library.get(media.library_id)
                    if storage is None:
                        storage = MEDIA_PROVIDER_REGISTRY.storage_for(handle.library)
                        storage_by_library[media.library_id] = storage
                    health = MediaPlaybackHealthService.probe_and_write(
                        media,
                        storage=storage,
                    )
                    stats["updated_media"] += 1
                    if health.status == STATUS_INVALID:
                        stats["invalid_media"] += 1
                    elif health.status == STATUS_UNKNOWN:
                        stats["unknown_media"] += 1
                    elif health.status == STATUS_OK:
                        stats["ok_media"] += 1
            except MediaOperationBusy:
                stats["skipped_media"] += 1
            except Exception as exc:
                stats["failed_media"] += 1
                logger.warning(
                    "Media playback health backfill failed media_id={} detail={}",
                    media_id,
                    exc,
                )
            emit_progress(completed)

        return stats
