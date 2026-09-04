from __future__ import annotations

from typing import Any

from loguru import logger

from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import media_handle_for


class MediaFileHashBackfillService:
    TASK_KEY = "media_file_hash_backfill"

    @staticmethod
    def _missing_file_hash_condition():
        return Media.file_hash.is_null(True) | (Media.file_hash == "")

    @classmethod
    def _candidate_ids(cls) -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                Media.select(Media.id)
                .where(cls._missing_file_hash_condition())
                .order_by(Media.id)
                .tuples()
            )
        ]

    @classmethod
    def backfill_missing_file_hashes(cls, *, reporter) -> dict[str, Any]:
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
                        .where(Media.id == media_id, cls._missing_file_hash_condition())
                        .get_or_none()
                    )
                    if media is None:
                        stats["skipped_media"] += 1
                        reporter.emit(
                            current=completed, total=len(media_ids), summary_patch=stats
                        )
                        continue

                    try:
                        handle = media_handle_for(media)
                        storage = storage_by_library.get(media.library_id)
                        if storage is None:
                            storage = MEDIA_PROVIDER_REGISTRY.storage_for(handle.library)
                            storage_by_library[media.library_id] = storage
                        file_hash = storage.compute_file_hash(media=handle)
                        if not isinstance(file_hash, str) or not file_hash:
                            raise ValueError("provider returned an invalid media file hash")
                        media.file_hash = file_hash
                        media.save(only=[Media.file_hash])
                    except Exception as exc:
                        stats["failed_media"] += 1
                        logger.warning(
                            "Media file hash backfill failed media_id={} library_id={} detail={}",
                            media.id,
                            media.library_id,
                            exc,
                        )
                    else:
                        stats["updated_media"] += 1
                    reporter.emit(current=completed, total=len(media_ids), summary_patch=stats)

            except MediaOperationBusy:
                stats["skipped_media"] += 1
                reporter.emit(current=completed, total=len(media_ids), summary_patch=stats)

        return stats
