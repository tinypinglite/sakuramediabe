from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger
from peewee import fn

from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Media, MediaLibrary, MediaThumbnail
from src.model.enums import MediaLibraryBackend
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.backend_registry import ThumbnailBackendRegistry
from src.service.playback.thumbnails.contracts import (
    ThumbnailBackendUnavailable,
    ThumbnailDeferred,
)


@dataclass(frozen=True)
class ThumbnailGenerationOutcome:
    """单条媒体的领域收口结果，供两条执行泳道统一汇总。"""

    state: str
    generated_count: int = 0
    error_code: str | None = None


class MediaThumbnailTaskService:
    """按 Media 自有状态生成缩略图，并让媒体级失败在有限次数内收口。"""

    TASK_KEY = "media_thumbnail_generation"
    MAX_FAILURE_ATTEMPTS = 2
    FAILURE_RETRY_BACKOFF_BASE_SECONDS = 15 * 60
    FAILURE_RETRY_BACKOFF_MAX_SECONDS = 24 * 60 * 60
    # 下列错误由媒体本身决定，重试不会改变结果，首次即可明确终态。
    TERMINAL_ERROR_CODES = frozenset(
        {
            "cloud115_locator_missing",
            "hls_clean_frame_missing",
            "hls_video_stream_missing",
            "thumbnail_generation_empty",
            "thumbnail_generation_insufficient_count",
            "thumbnail_generation_unparseable_filenames",
            "video_file_missing",
            "video_stream_missing",
        }
    )

    @staticmethod
    def _thumbnail_exists_query():
        return MediaThumbnail.select(MediaThumbnail.id).where(
            MediaThumbnail.media == Media.id
        )

    @classmethod
    def _missing_thumbnail_condition(cls):
        return ~fn.EXISTS(cls._thumbnail_exists_query())

    @staticmethod
    def _has_usable_fingerprint_condition():
        return (
            Media.content_fingerprint.is_null(False)
            & (Media.content_fingerprint != "")
        )

    @classmethod
    def _source_changed_condition(cls):
        """内容版本变化后重新打开旧状态，终态失败不会永久绑定到新文件。"""
        return cls._has_usable_fingerprint_condition() & (
            Media.thumbnail_source_fingerprint.is_null(True)
            | (Media.thumbnail_source_fingerprint != Media.content_fingerprint)
        )

    @classmethod
    def _candidate_query(
        cls,
        *,
        backend_lane: str | None = None,
    ):
        now = utc_now_for_db()
        normal_state = (
            Media.thumbnail_generation_state.in_(
                (
                    Media.THUMBNAIL_STATE_PENDING,
                    # 成功产物被人工清理时，重新生成而不是永久卡在 succeeded。
                    Media.THUMBNAIL_STATE_SUCCEEDED,
                )
            )
            | cls._source_changed_condition()
            | (
                (Media.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT)
                & (
                    Media.thumbnail_next_retry_at.is_null(True)
                    | (Media.thumbnail_next_retry_at <= now)
                )
            )
        )
        query = (
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(
                Media.valid == True,
                cls._has_usable_fingerprint_condition(),
                cls._missing_thumbnail_condition(),
                normal_state,
            )
            .order_by(Media.id)
        )
        if backend_lane == "cloud115":
            query = query.where(
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value
            )
        elif backend_lane == "local":
            query = query.where(
                MediaLibrary.backend != MediaLibraryBackend.CLOUD115.value
            )
        return query

    @classmethod
    def _candidate_ids(cls, backend_lane: str) -> list[int]:
        return [
            int(media_id)
            for (media_id,) in cls._candidate_query(backend_lane=backend_lane).tuples()
        ]

    @classmethod
    def _count_state(cls, state: str) -> int:
        return (
            Media.select(Media.id)
            .where(
                cls._missing_thumbnail_condition(),
                Media.thumbnail_generation_state == state,
                ~cls._source_changed_condition(),
            )
            .count()
        )

    @classmethod
    def count_pending_media(cls) -> int:
        """返回当前可执行的媒体数；到期 retry_wait 也属于本轮待处理。"""
        return cls._candidate_query().count()

    @classmethod
    def count_retry_wait_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_RETRY_WAIT)

    @classmethod
    def count_terminal_failed_media(cls) -> int:
        return cls._count_state(Media.THUMBNAIL_STATE_TERMINAL)

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
    def generate_for_media(cls, media: Media) -> int:
        backend = ThumbnailBackendRegistry.for_media(media)
        ensure_available = getattr(backend, "ensure_available", None)
        if callable(ensure_available):
            ensure_available()
        prepared = backend.prepare(media)
        logger.info(
            "Generating media thumbnails media_id={} movie_number={} source={}",
            media.id,
            media.movie_number,
            prepared.source_label,
        )
        started_at = time.time()
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
            logger.info(
                "Generated media thumbnails media_id={} backend={} "
                "generated_thumbnails={} elapsed_ms={}",
                media.id,
                backend.key,
                generated_count,
                int((time.time() - started_at) * 1000),
            )
            return generated_count

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
        return generated_count

    @staticmethod
    def _error_code(exc: Exception) -> str:
        error_code = getattr(exc, "error_code", None)
        if isinstance(error_code, str) and error_code.strip():
            return error_code.strip()[:64]
        detail = str(exc).strip()
        if detail:
            return detail.split(maxsplit=1)[0].split(":", maxsplit=1)[0][:64]
        return type(exc).__name__.lower()[:64]

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        return (str(exc).strip() or type(exc).__name__)[:4000]

    @classmethod
    def _write_state(
        cls,
        media: Media,
        *,
        state: str,
        attempt_count: int,
        deferred_count: int,
        next_retry_at,
        error_code: str | None,
        error_detail: str | None,
        terminal_at,
    ) -> None:
        # 状态更新集中在单条 UPDATE：不把并发执行时的旧 Media 实例整行写回数据库。
        Media.update(
            thumbnail_generation_state=state,
            thumbnail_attempt_count=attempt_count,
            thumbnail_deferred_count=deferred_count,
            thumbnail_next_retry_at=next_retry_at,
            thumbnail_last_error_code=error_code,
            thumbnail_last_error=error_detail,
            thumbnail_terminal_at=terminal_at,
            thumbnail_source_fingerprint=media.content_fingerprint,
            updated_at=utc_now_for_db(),
        ).where(Media.id == media.id).execute()

    @classmethod
    def _mark_succeeded(cls, media: Media) -> None:
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_SUCCEEDED,
            attempt_count=0,
            deferred_count=0,
            next_retry_at=None,
            error_code=None,
            error_detail=None,
            terminal_at=None,
        )

    @classmethod
    def _mark_deferred(cls, media: Media, exc: ThumbnailDeferred) -> bool:
        """返回是否因达到延后上限转为 terminal。"""
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0) + 1
        deferred_count = int(media.thumbnail_deferred_count or 0) + 1
        error_code = cls._error_code(exc)
        error_detail = cls._error_detail(exc)
        if deferred_count > exc.max_deferred_attempts:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=deferred_count,
                next_retry_at=None,
                error_code=error_code,
                error_detail=error_detail,
                terminal_at=now,
            )
            return True

        backoff_seconds = min(
            exc.deferred_backoff_base_seconds * deferred_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=deferred_count,
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=error_code,
            error_detail=error_detail,
            terminal_at=None,
        )
        return False

    @classmethod
    def _mark_failure(cls, media: Media, exc: Exception) -> bool:
        """媒体错误最多重试一次；确定性错误直接进入 terminal。"""
        now = utc_now_for_db()
        attempt_count = int(media.thumbnail_attempt_count or 0) + 1
        error_code = cls._error_code(exc)
        error_detail = cls._error_detail(exc)
        is_terminal = (
            error_code in cls.TERMINAL_ERROR_CODES
            or attempt_count >= cls.MAX_FAILURE_ATTEMPTS
        )
        if is_terminal:
            cls._write_state(
                media,
                state=Media.THUMBNAIL_STATE_TERMINAL,
                attempt_count=attempt_count,
                deferred_count=int(media.thumbnail_deferred_count or 0),
                next_retry_at=None,
                error_code=error_code,
                error_detail=error_detail,
                terminal_at=now,
            )
            return True

        backoff_seconds = min(
            cls.FAILURE_RETRY_BACKOFF_BASE_SECONDS * attempt_count,
            cls.FAILURE_RETRY_BACKOFF_MAX_SECONDS,
        )
        cls._write_state(
            media,
            state=Media.THUMBNAIL_STATE_RETRY_WAIT,
            attempt_count=attempt_count,
            deferred_count=int(media.thumbnail_deferred_count or 0),
            next_retry_at=now + timedelta(seconds=backoff_seconds),
            error_code=error_code,
            error_detail=error_detail,
            terminal_at=None,
        )
        return False

    @classmethod
    def _generate_one(cls, media_id: int) -> ThumbnailGenerationOutcome:
        ensure_database_ready()
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid:
            return ThumbnailGenerationOutcome("skipped")
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            cls._mark_succeeded(media)
            return ThumbnailGenerationOutcome("skipped")
        # 候选 SQL 已严格排除缺指纹媒体；这里防止查询后字段被并发清空。
        if not media.content_fingerprint:
            return ThumbnailGenerationOutcome("skipped")
        try:
            generated_count = cls.generate_for_media(media)
        except ThumbnailBackendUnavailable as exc:
            logger.warning(
                "Media thumbnail backend unavailable media_id={} code={} detail={}",
                media_id,
                exc.error_code,
                exc,
            )
            return ThumbnailGenerationOutcome(
                "backend_unavailable",
                error_code=exc.error_code,
            )
        except ThumbnailDeferred as exc:
            terminal = cls._mark_deferred(media, exc)
            logger.warning(
                "Media thumbnail generation deferred media_id={} code={} terminal={} detail={}",
                media_id,
                exc.error_code,
                terminal,
                exc,
            )
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "deferred",
                error_code=exc.error_code,
            )
        except Exception as exc:
            # 若并发路径已经持久化了缩略图，产物事实优先，不再写回失败状态。
            if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
                cls._mark_succeeded(media)
                return ThumbnailGenerationOutcome("succeeded")
            terminal = cls._mark_failure(media, exc)
            logger.exception(
                "Media thumbnail generation failed media_id={} code={} terminal={}",
                media_id,
                cls._error_code(exc),
                terminal,
            )
            return ThumbnailGenerationOutcome(
                "terminal_failed" if terminal else "retryable_failed",
                error_code=cls._error_code(exc),
            )

        cls._mark_succeeded(media)
        return ThumbnailGenerationOutcome("succeeded", generated_count=generated_count)

    @classmethod
    def generate_pending_thumbnails(
        cls,
        *,
        reporter,
    ) -> dict[str, Any]:
        started_at = time.time()
        cloud_ids = cls._candidate_ids("cloud115")
        local_ids = cls._candidate_ids("local")
        all_ids = cloud_ids + local_ids
        stats: dict[str, Any] = {
            "pending_media": len(all_ids),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "deferred_media": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
            "failed_media_ids": [],
            "terminal_failed_media_ids": [],
            "backend_failed_lanes": 0,
            "backend_deferred_media": 0,
            "backend_failure_codes": [],
            "skipped_media": 0,
        }
        completed = 0
        failed_backend_lanes: set[str] = set()

        def record(media_id: int, outcome: ThumbnailGenerationOutcome) -> None:
            nonlocal completed
            if outcome.state == "succeeded":
                stats["successful_media"] += 1
                stats["generated_thumbnails"] += outcome.generated_count
            elif outcome.state == "deferred":
                stats["deferred_media"] += 1
            elif outcome.state == "retryable_failed":
                stats["retryable_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
            elif outcome.state == "terminal_failed":
                stats["terminal_failed_media"] += 1
                stats["failed_media_ids"].append(media_id)
                stats["terminal_failed_media_ids"].append(media_id)
            elif outcome.state == "skipped":
                stats["skipped_media"] += 1
            completed += 1
            reporter.emit(current=completed, total=len(all_ids), summary_patch=stats)

        def mark_backend_unavailable(
            *,
            lane: str,
            remaining_ids: list[int],
            exc: ThumbnailBackendUnavailable,
        ) -> None:
            nonlocal completed
            # 本地并发泳道可能有多个已在飞行的媒体同时感知到同一个后端故障；泳道只计一次，
            # 但每一条未处理媒体都要进入本轮 deferred 统计，便于前端正确显示进度。
            if lane not in failed_backend_lanes:
                failed_backend_lanes.add(lane)
                stats["backend_failed_lanes"] += 1
                stats["backend_failure_codes"].append(exc.error_code)
            stats["backend_deferred_media"] += len(remaining_ids)
            completed += len(remaining_ids)
            logger.warning(
                "Thumbnail backend lane paused lane={} media_count={} code={} detail={}",
                lane,
                len(remaining_ids),
                exc.error_code,
                exc,
            )
            reporter.emit(current=completed, total=len(all_ids), summary_patch=stats)

        def run_cloud_lane() -> None:
            if not cloud_ids:
                return
            try:
                ThumbnailBackendRegistry.ensure_available(MediaLibraryBackend.CLOUD115.value)
            except ThumbnailBackendUnavailable as exc:
                mark_backend_unavailable(
                    lane="cloud115", remaining_ids=cloud_ids, exc=exc
                )
                return
            for index, media_id in enumerate(cloud_ids):
                outcome = cls._generate_one(media_id)
                if outcome.state == "backend_unavailable":
                    mark_backend_unavailable(
                        lane="cloud115",
                        remaining_ids=cloud_ids[index:],
                        exc=ThumbnailBackendUnavailable(
                            "Cloud115 thumbnail backend became unavailable",
                            error_code=outcome.error_code or "thumbnail_backend_unavailable",
                        ),
                    )
                    return
                record(media_id, outcome)

        def run_local_lane() -> None:
            if not local_ids:
                return
            try:
                ThumbnailBackendRegistry.ensure_available(MediaLibraryBackend.LOCAL.value)
            except ThumbnailBackendUnavailable as exc:
                mark_backend_unavailable(lane="local", remaining_ids=local_ids, exc=exc)
                return
            with ThreadPoolExecutor(
                max_workers=max(settings.media.max_thumbnail_process_count, 1),
                thread_name_prefix="media-thumbnail",
            ) as pool:
                futures = {
                    pool.submit(cls._generate_one, media_id): media_id for media_id in local_ids
                }
                for future in as_completed(futures):
                    media_id = futures[future]
                    outcome = future.result()
                    if outcome.state == "backend_unavailable":
                        # 已在飞行的本地任务允许完成；后续轮次会在预检阶段整体恢复或暂停。
                        mark_backend_unavailable(
                            lane="local",
                            remaining_ids=[media_id],
                            exc=ThumbnailBackendUnavailable(
                                "Local thumbnail backend became unavailable",
                                error_code=outcome.error_code
                                or "thumbnail_backend_unavailable",
                            ),
                        )
                    else:
                        record(media_id, outcome)

        # 115 源受远端限速约束，保持串行；本地源继续使用配置的并发度。
        run_cloud_lane()
        run_local_lane()

        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} "
            "generated_thumbnails={} deferred_media={} retryable_failed_media={} "
            "terminal_failed_media={} backend_failed_lanes={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["deferred_media"],
            stats["retryable_failed_media"],
            stats["terminal_failed_media"],
            stats["backend_failed_lanes"],
            int((time.time() - started_at) * 1000),
        )
        return stats
