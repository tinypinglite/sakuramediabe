from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger
from peewee import fn

from src.common.database import ensure_database_ready
from src.config.config import settings
from src.model import Media, MediaLibrary, MediaThumbnail, ResourceTaskState
from src.model.enums import MediaLibraryBackend
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.backend_registry import ThumbnailBackendRegistry
from src.service.playback.thumbnails.contracts import ThumbnailDeferred
from src.service.system import ActivityService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class MediaThumbnailTaskService:
    TASK_KEY = "media_thumbnail_generation"
    THUMBNAIL_MAX_RETRIES = 2
    INTERRUPTED_GENERATION_ERROR_MESSAGE = "媒体缩略图生成任务中断，等待重试"

    @staticmethod
    def _ensure_worker_database_ready() -> None:
        ensure_database_ready()

    @classmethod
    def pending_media_ids(cls) -> list[int]:
        matching_state_query = ResourceTaskState.select(ResourceTaskState.id).where(
            ResourceTaskState.task_key == cls.TASK_KEY,
            ResourceTaskState.resource_type == "media",
            ResourceTaskState.resource_id == Media.id,
        )
        query = (
            Media.select(Media.id)
            .where(
                Media.valid == True,
                (
                    ~fn.EXISTS(matching_state_query)
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state == ResourceTaskStateService.STATE_PENDING
                        )
                    )
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state == ResourceTaskStateService.STATE_FAILED,
                            ResourceTaskStateService.build_retryable_extra_condition(
                                ResourceTaskState.extra
                            ),
                        )
                    )
                ),
            )
            .order_by(Media.id)
        )
        return [item.id for item in query]

    @staticmethod
    def cloud115_media_ids(media_ids: list[int]) -> set[int]:
        if not media_ids:
            return set()
        query = (
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(
                Media.id.in_(media_ids),
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value,
            )
        )
        return {media.id for media in query}

    @classmethod
    def count_pending_media(cls) -> int:
        return len(cls.pending_media_ids())

    @classmethod
    def mark_success(cls, media: Media) -> None:
        ResourceTaskStateService.mark_succeeded(
            cls.TASK_KEY, media.id, extra_patch={"terminal": False}
        )

    @classmethod
    def mark_failure(cls, media: Media, error: str, *, terminal: bool = False) -> str:
        task_state = ResourceTaskStateService.get_state_or_default(cls.TASK_KEY, media.id)
        is_terminal = bool(
            terminal or task_state.attempt_count >= cls.THUMBNAIL_MAX_RETRIES
        )
        ResourceTaskStateService.mark_failed(
            cls.TASK_KEY,
            media.id,
            error,
            extra_patch={"terminal": is_terminal},
        )
        return "terminal_failed_media" if is_terminal else "retryable_failed_media"

    @staticmethod
    def failure_type(result_key: str) -> str:
        return "terminal" if result_key == "terminal_failed_media" else "retryable"

    @classmethod
    def log_aborted(cls, media: Media, reason: str, result_key: str) -> None:
        task_state = ResourceTaskStateService.get_state_or_default(cls.TASK_KEY, media.id)
        logger.warning(
            "Generate media thumbnails aborted media_id={} reason={} failure_type={} retry_count={}",
            media.id,
            reason,
            cls.failure_type(result_key),
            task_state.attempt_count,
        )

    @classmethod
    def recover_interrupted_running_media(cls, *, error_message: str | None = None) -> int:
        normalized_error = (
            (error_message or "").strip()
            or cls.INTERRUPTED_GENERATION_ERROR_MESSAGE
        )
        return ResourceTaskStateService.recover_running_records(
            cls.TASK_KEY, normalized_error
        )

    @staticmethod
    def minimum_acceptable_count(expected_count: int) -> int:
        if expected_count <= 0:
            return 0
        return max(1, int(expected_count * 0.85))

    @staticmethod
    def insufficient_count_error(
        *,
        expected_count: int,
        minimum_count: int,
        actual_count: int,
        generation_error: Exception | None,
    ) -> str:
        message = (
            f"thumbnail_generation_insufficient_count expected={expected_count} "
            f"minimum={minimum_count} actual={actual_count}"
        )
        if generation_error is not None:
            message = f"{message} cause=pyav={generation_error}"
        return message

    @classmethod
    def process_media(cls, media_id: int) -> dict[str, int]:
        cls._ensure_worker_database_ready()
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid:
            return {}
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            cls.mark_success(media)
            logger.info(
                "Skipping media thumbnail generation media_id={} reason=thumbnails_already_exist",
                media.id,
            )
            return {}
        if not media.content_fingerprint:
            ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
            error_key = cls.mark_failure(
                media, "content_fingerprint_missing", terminal=True
            )
            cls.log_aborted(media, "content_fingerprint_missing", error_key)
            return {error_key: 1}

        backend = ThumbnailBackendRegistry.for_media(media)
        try:
            prepared = backend.prepare(media)
        except ThumbnailDeferred as exc:
            logger.warning(
                "Deferred thumbnail generation media_id={} backend={} detail={} retry_count={}",
                media.id,
                backend.key,
                exc,
                ResourceTaskStateService.get_state_or_default(
                    cls.TASK_KEY, media.id
                ).attempt_count,
            )
            return {"deferred_media": 1}
        except Exception as exc:
            ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
            error_key = cls.mark_failure(media, str(exc))
            cls.log_aborted(media, str(exc), error_key)
            return {error_key: 1}

        ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
        logger.info(
            "Generating media thumbnails media_id={} movie_number={} source={}",
            media.id,
            media.movie_number,
            prepared.source_label,
        )
        started_at = time.time()
        try:
            webp_dir = ThumbnailArtifactService.thumbnail_directory(media)
            ThumbnailArtifactService.clear_directory(webp_dir)
            generation = backend.generate(prepared, webp_dir)
            if generation.first_error is not None:
                logger.warning(
                    "Thumbnail backend reported error media_id={} backend={} detail={}",
                    media.id,
                    backend.key,
                    generation.first_error,
                )

            parseable_files, total_webp_count = ThumbnailArtifactService.collect_webp_files(
                webp_dir
            )
            parseable_count = len(parseable_files)
            minimum_count = cls.minimum_acceptable_count(prepared.expected_count)
            if prepared.expected_count > 0 and parseable_count >= minimum_count:
                generated_count = ThumbnailArtifactService.persist(media, parseable_files)
                if generated_count == 0:
                    raise RuntimeError("thumbnail_generation_unparseable_filenames")
                cls.mark_success(media)
                logger.info(
                    "Generated media thumbnails media_id={} backend={} generated_thumbnails={} elapsed_ms={}",
                    media.id,
                    backend.key,
                    generated_count,
                    int((time.time() - started_at) * 1000),
                )
                return {
                    "successful_media": 1,
                    "generated_thumbnails": generated_count,
                }

            if prepared.expected_count > 0 and generation.first_error is not None:
                raise RuntimeError(
                    cls.insufficient_count_error(
                        expected_count=prepared.expected_count,
                        minimum_count=minimum_count,
                        actual_count=parseable_count,
                        generation_error=generation.first_error,
                    )
                )
            if generation.first_error is not None:
                raise generation.first_error
            if total_webp_count == 0:
                raise RuntimeError("thumbnail_generation_empty")
            if parseable_count == 0:
                raise RuntimeError("thumbnail_generation_unparseable_filenames")

            generated_count = ThumbnailArtifactService.persist(media, parseable_files)
            if generated_count == 0:
                raise RuntimeError("thumbnail_generation_unparseable_filenames")
            cls.mark_success(media)
            return {"successful_media": 1, "generated_thumbnails": generated_count}
        except Exception as exc:
            error_key = cls.mark_failure(media, str(exc))
            task_state = ResourceTaskStateService.get_state_or_default(
                cls.TASK_KEY, media.id
            )
            logger.warning(
                "Generate media thumbnails failed media_id={} detail={} failure_type={} retry_count={}",
                media.id,
                exc,
                cls.failure_type(error_key),
                task_state.attempt_count,
            )
            return {error_key: 1}

    @staticmethod
    def emit_progress(progress_callback, **payload) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    @classmethod
    def generate_pending_thumbnails(cls, progress_callback=None) -> dict[str, int]:
        media_ids = cls.pending_media_ids()
        started_at = time.time()
        stats = {
            "pending_media": len(media_ids),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "deferred_media": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
        }
        if not media_ids:
            logger.info("No pending media for thumbnail generation")
            return stats
        cls.emit_progress(
            progress_callback,
            current=0,
            total=len(media_ids),
            text="开始生成媒体缩略图",
            summary_patch=stats,
        )
        cloud_ids = cls.cloud115_media_ids(media_ids)
        local_ids = [media_id for media_id in media_ids if media_id not in cloud_ids]
        ordered_cloud_ids = [media_id for media_id in media_ids if media_id in cloud_ids]
        completed_count = 0

        def record_result(result: dict[str, int]) -> None:
            nonlocal completed_count
            completed_count += 1
            for key, value in result.items():
                stats[key] += value
            cls.emit_progress(
                progress_callback,
                current=completed_count,
                total=len(media_ids),
                text=f"已处理缩略图任务 {completed_count}/{len(media_ids)}",
                summary_patch=stats,
            )

        with ThreadPoolExecutor(
            max_workers=settings.media.max_thumbnail_process_count,
            thread_name_prefix="media-thumbnail-local",
        ) as executor:
            local_futures = [
                executor.submit(
                    ActivityService.wrap_current_task_run_context(cls.process_media),
                    media_id,
                )
                for media_id in local_ids
            ]
            for media_id in ordered_cloud_ids:
                record_result(cls.process_media(media_id))
            for future in as_completed(local_futures):
                record_result(future.result())
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} "
            "generated_thumbnails={} deferred_media={} retryable_failed_media={} "
            "terminal_failed_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["deferred_media"],
            stats["retryable_failed_media"],
            stats["terminal_failed_media"],
            int((time.time() - started_at) * 1000),
        )
        return stats
