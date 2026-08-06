from __future__ import annotations

import time

from loguru import logger

from src.config.config import settings
from src.model import Media, MediaLibrary, MediaThumbnail
from src.model.enums import MediaLibraryBackend
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.backend_registry import ThumbnailBackendRegistry
from src.service.playback.thumbnails.contracts import ThumbnailDeferred
from src.service.system.resource_task_runner import (
    ResourceTaskLedger,
    ResourceTaskRunner,
    ResourceTaskSpec,
    RetryPolicy,
    TaskItemDeferred,
    TaskItemError,
)


class MediaThumbnailTaskService:
    """媒体缩略图生成（已迁 kernel 记账，任务架构 Wave 2）。

    双泳道 = 两次 Runner 执行：cloud115 媒体串行（远端限速），本地媒体按
    ``max_thumbnail_process_count`` 并发（任务内并发由内核线程池接管，
    ctx 显式传参，旧的 ``wrap_current_task_run_context`` ContextVar 链路退役）。

    失败分类：缺内容指纹 → failed_terminal；生成失败按预算退避（2 次/轮后
    exhausted，可经重置接口重开）；源暂不可用（HLS 未就绪等）→ deferred 不耗预算。
    """

    TASK_KEY = "media_thumbnail_generation"
    THUMBNAIL_MAX_RETRIES = 2
    INTERRUPTED_GENERATION_ERROR_MESSAGE = "媒体缩略图生成任务中断，等待重试"
    RETRY_POLICY = RetryPolicy(
        max_attempts=THUMBNAIL_MAX_RETRIES,
        backoff_base_seconds=900,
        backoff_max_seconds=86400,
    )
    # 生成链路里可识别的确定性错误标记（历史裸字符串常量），映射为结构化 error_code。
    KNOWN_ERROR_CODES = (
        "thumbnail_generation_empty",
        "thumbnail_generation_unparseable_filenames",
        "thumbnail_generation_insufficient_count",
    )

    # -------- 候选查询 --------

    @classmethod
    def _candidate_query(
        cls,
        state_condition,
        *,
        backend_lane: str | None = None,
        only_ids=None,
    ):
        query = (
            # 候选只取 id（count_pending_media 复用同一查询做 .count() 兼容）；
            # 完整模型由内核按 CANDIDATE_BATCH_SIZE 分批加载。
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(
                Media.valid == True,
                state_condition(cls.TASK_KEY, "media", Media.id),
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
        if only_ids:
            query = query.where(Media.id.in_(list(only_ids)))
        return query

    @classmethod
    def count_pending_media(cls) -> int:
        return cls._candidate_query(ResourceTaskLedger.eligible_state_condition).count()

    @classmethod
    def recover_interrupted_running_media(cls, *, error_message: str | None = None) -> int:
        normalized_error = (
            (error_message or "").strip() or cls.INTERRUPTED_GENERATION_ERROR_MESSAGE
        )
        return ResourceTaskLedger.recover_running(
            cls.TASK_KEY, error_message=normalized_error
        )

    # -------- 生成核心 --------

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
    def _classify_error_code(cls, message: str) -> str:
        for known_code in cls.KNOWN_ERROR_CODES:
            if message.startswith(known_code):
                return known_code
        return "thumbnail_generation_failed"

    @classmethod
    def generate_for_media(cls, media: Media) -> int:
        """备好源并生成缩略图，返回入库张数。失败抛原始异常，由 _process_one 分类。

        独立成方法既是测试替换点，也隔离"生成"与"记账"两层关注点。
        """
        backend = ThumbnailBackendRegistry.for_media(media)
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

    @classmethod
    def _process_one(cls, ctx, media_row: Media) -> None:
        media = Media.get_or_none(Media.id == media_row.id)
        if media is None or not media.valid:
            raise TaskItemDeferred(
                "media_missing_or_invalid", "媒体已删除或失效，等待巡检收敛"
            )
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            logger.info(
                "Skipping media thumbnail generation media_id={} "
                "reason=thumbnails_already_exist",
                media.id,
            )
            return
        if not media.content_fingerprint:
            raise TaskItemError(
                "content_fingerprint_missing",
                "缺少内容指纹，无法定位缩略图目录",
                retryable=False,
            )
        try:
            generated_count = cls.generate_for_media(media)
        except (TaskItemDeferred, TaskItemError):
            raise
        except ThumbnailDeferred as exc:
            raise TaskItemDeferred("thumbnail_source_deferred", str(exc)) from exc
        except Exception as exc:
            raise TaskItemError(cls._classify_error_code(str(exc)), str(exc)) from exc
        counters = ctx.shared
        if counters is not None:
            counters["generated_thumbnails"] += generated_count

    # -------- 批量入口 --------

    @classmethod
    def _build_spec(cls, backend_lane: str, concurrency: int) -> ResourceTaskSpec:
        return ResourceTaskSpec(
            task_key=cls.TASK_KEY,
            resource_type="media",
            retry=cls.RETRY_POLICY,
            select_candidates=lambda state_condition, only_ids=None: cls._candidate_query(
                state_condition, backend_lane=backend_lane, only_ids=only_ids
            ),
            process_one=cls._process_one,
            setup_run=lambda _ctx: {"generated_thumbnails": 0},
            concurrency=concurrency,
            resource_model=Media,
        )

    @classmethod
    def generate_pending_thumbnails(
        cls, *, reporter, only_ids: list[int] | None = None
    ) -> dict[str, int]:
        started_at = time.time()
        # cloud115 泳道先行且串行（远端限速），本地泳道随后并发。
        cloud_stats = ResourceTaskRunner.run(
            cls._build_spec("cloud115", 1), reporter, only_ids=only_ids
        )
        local_stats = ResourceTaskRunner.run(
            cls._build_spec("local", settings.media.max_thumbnail_process_count),
            reporter,
            only_ids=only_ids,
        )

        def merged(key: str) -> int:
            return int(cloud_stats.get(key, 0)) + int(local_stats.get(key, 0))

        generated_thumbnails = sum(
            int((stats.get("shared") or {}).get("generated_thumbnails", 0))
            for stats in (cloud_stats, local_stats)
        )
        stats = {
            "pending_media": merged("candidate_count"),
            "successful_media": merged("succeeded_count"),
            "generated_thumbnails": generated_thumbnails,
            "deferred_media": merged("deferred_count"),
            "retryable_failed_media": merged("failed_retryable_count"),
            "terminal_failed_media": merged("failed_terminal_count"),
            "exhausted_media": merged("exhausted_count"),
        }
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} "
            "generated_thumbnails={} deferred_media={} retryable_failed_media={} "
            "terminal_failed_media={} exhausted_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["deferred_media"],
            stats["retryable_failed_media"],
            stats["terminal_failed_media"],
            stats["exhausted_media"],
            int((time.time() - started_at) * 1000),
        )
        return stats
