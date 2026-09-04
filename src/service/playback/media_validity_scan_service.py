from __future__ import annotations

from typing import Any

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary, MediaThumbnail
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback.operation_locks import (
    LIBRARY_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import library_handle_for


class MediaValidityScanService:
    """Reconcile stored Media validity against provider-managed file inventories."""

    TASK_KEY = "media_file_scan"

    @staticmethod
    def _max_media_id() -> int:
        return (
            Media.select(Media.id).order_by(Media.id.desc()).limit(1).scalar()
            or 0
        )

    @classmethod
    def _library_media_query(cls, library: MediaLibrary, max_media_id: int):
        return (
            Media.select()
            .where(Media.library == library, Media.id <= max_media_id)
            .order_by(Media.id)
        )

    @classmethod
    def _managed_ref_keys(cls, library: MediaLibrary):
        storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
        scan_managed_media_ref_keys = getattr(storage, "scan_managed_media_ref_keys", None)
        managed_media_ref_key = getattr(storage, "managed_media_ref_key", None)
        if not callable(scan_managed_media_ref_keys) or not callable(managed_media_ref_key):
            return None
        return managed_media_ref_key, scan_managed_media_ref_keys()

    @staticmethod
    def _revival_thumbnail_values(media: Media) -> dict:
        has_thumbnail = MediaThumbnail.select(MediaThumbnail.id).where(
            MediaThumbnail.media == media.id
        ).exists()
        return {
            Media.thumbnail_generation_state: (
                Media.THUMBNAIL_STATE_SUCCEEDED
                if has_thumbnail
                else Media.THUMBNAIL_STATE_PENDING
            ),
            Media.thumbnail_attempt_count: 0,
            Media.thumbnail_deferred_count: 0,
            Media.thumbnail_next_retry_at: None,
            Media.thumbnail_last_error_code: None,
            Media.thumbnail_last_error: None,
            Media.thumbnail_terminal_at: None,
        }

    @classmethod
    def scan_media_validity(cls, *, reporter) -> dict[str, Any]:
        max_media_id = cls._max_media_id()
        total_media = Media.select().where(Media.id <= max_media_id).count()
        stats: dict[str, Any] = {
            "total_media": total_media,
            "scanned_media": 0,
            "updated_media": 0,
            "unchanged_media": 0,
            "invalidated_media": 0,
            "revived_media": 0,
            "skipped_media": 0,
            "failed_media": 0,
            "scanned_libraries": 0,
            "unsupported_libraries": 0,
            "failed_libraries": 0,
        }
        completed = 0

        for library in MediaLibrary.select().order_by(MediaLibrary.id):
            try:
                with media_operation_lock(LIBRARY_LOCK, library.id):
                    library = MediaLibrary.get_or_none(MediaLibrary.id == library.id)
                    if library is None:
                        continue
                    media_items = cls._library_media_query(library, max_media_id)
                    if not media_items.exists():
                        continue
                    try:
                        managed_inventory = cls._managed_ref_keys(library)
                    except Exception as exc:
                        stats["failed_libraries"] += 1
                        logger.warning(
                            "Media validity scan skipped library_id={} provider_key={} detail={}",
                            library.id,
                            library.provider_key,
                            exc,
                        )
                        managed_inventory = None
                    else:
                        if managed_inventory is None:
                            stats["unsupported_libraries"] += 1
                        else:
                            stats["scanned_libraries"] += 1

                    for media in media_items:
                        completed += 1
                        if managed_inventory is None:
                            stats["skipped_media"] += 1
                            reporter.emit(current=completed, total=total_media, summary_patch=stats)
                            continue

                        stats["scanned_media"] += 1
                        try:
                            managed_media_ref_key, managed_ref_keys = managed_inventory
                            exists = managed_media_ref_key(media_ref=media.storage_ref) in managed_ref_keys
                        except (ValueError, ProviderOperationError) as exc:
                            stats["failed_media"] += 1
                            logger.warning(
                                "Media validity scan skipped invalid storage ref media_id={} library_id={} detail={}",
                                media.id,
                                library.id,
                                exc,
                            )
                        else:
                            valid_before = bool(media.valid)
                            if valid_before == exists:
                                stats["unchanged_media"] += 1
                            else:
                                updates = {Media.valid: exists, Media.updated_at: utc_now_for_db()}
                                if exists:
                                    updates.update(cls._revival_thumbnail_values(media))
                                updated = (
                                    Media.update(updates)
                                    .where(Media.id == media.id, Media.valid == valid_before)
                                    .execute()
                                )
                                if updated != 1:
                                    stats["skipped_media"] += 1
                                else:
                                    stats["updated_media"] += 1
                                    if exists:
                                        stats["revived_media"] += 1
                                    else:
                                        stats["invalidated_media"] += 1
                        reporter.emit(current=completed, total=total_media, summary_patch=stats)

            except MediaOperationBusy:
                skipped = cls._library_media_query(library, max_media_id).count()
                stats["skipped_media"] += skipped
                completed += skipped
                reporter.emit(current=completed, total=total_media, summary_patch=stats)

        return stats
