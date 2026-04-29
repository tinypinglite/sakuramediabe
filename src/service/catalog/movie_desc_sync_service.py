from __future__ import annotations

from loguru import logger
from peewee import fn

from src.model import Movie, ResourceTaskState
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class MovieDescSyncService:
    TASK_KEY = "movie_desc_sync"
    INTERRUPTED_FETCH_ERROR_MESSAGE = "影片描述抓取任务中断，等待重试"

    def __init__(self, catalog_import_service: CatalogImportService | None = None):
        self.catalog_import_service = catalog_import_service or CatalogImportService()

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @staticmethod
    def _candidate_query():
        matching_state_query = ResourceTaskState.select(ResourceTaskState.id).where(
            ResourceTaskState.task_key == MovieDescSyncService.TASK_KEY,
            ResourceTaskState.resource_type == "movie",
            ResourceTaskState.resource_id == Movie.id,
        )
        retryable_failed_state_query = matching_state_query.where(
            ResourceTaskState.state == ResourceTaskStateService.STATE_FAILED,
            ResourceTaskStateService.build_retryable_extra_condition(ResourceTaskState.extra),
        )
        return (
            Movie.select(Movie)
            .where(
                Movie.desc == "",
                (
                    ~fn.EXISTS(matching_state_query)
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state == ResourceTaskStateService.STATE_PENDING
                        )
                    )
                    # terminal=true 的失败代表 DMM 已确认没有该番号，不再自动纳入候选。
                    | fn.EXISTS(retryable_failed_state_query)
                ),
            )
            .order_by(Movie.subscribed_at.is_null(), Movie.subscribed_at.asc(), Movie.id.asc())
        )

    @classmethod
    def recover_interrupted_running_movies(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_FETCH_ERROR_MESSAGE
        # 只回收遗留在 running 的描述抓取状态，避免误改未执行或已完成的影片。
        return ResourceTaskStateService.recover_running_records(cls.TASK_KEY, normalized_error)

    def run(
        self,
        *,
        batch_size: int | None = None,
        progress_callback=None,
    ) -> dict[str, int]:
        query = self._candidate_query()
        if batch_size is not None and int(batch_size) > 0:
            query = query.limit(int(batch_size))
        candidates = list(query)
        stats = {
            "candidate_movies": len(candidates),
            "processed_movies": 0,
            "succeeded_movies": 0,
            "failed_movies": 0,
            "updated_movies": 0,
            "skipped_movies": 0,
        }
        self._emit_progress(
            progress_callback,
            current=0,
            total=stats["candidate_movies"],
            text="开始回填影片描述",
            summary_patch=stats,
        )

        for movie in candidates:
            stats["processed_movies"] += 1
            latest_movie = Movie.get_by_id(movie.id)
            if latest_movie.desc:
                stats["skipped_movies"] += 1
                self._emit_progress(
                    progress_callback,
                    current=stats["processed_movies"],
                    total=stats["candidate_movies"],
                    text=f"跳过已有描述影片 {latest_movie.movie_number}",
                    summary_patch=stats,
                )
                continue

            if self.catalog_import_service.sync_movie_desc(latest_movie):
                stats["succeeded_movies"] += 1
                if latest_movie.desc:
                    stats["updated_movies"] += 1
                self._emit_progress(
                    progress_callback,
                    current=stats["processed_movies"],
                    total=stats["candidate_movies"],
                    text=f"回填描述成功 {latest_movie.movie_number}",
                    summary_patch=stats,
                )
                continue

            stats["failed_movies"] += 1
            logger.warning("Movie desc sync failed movie_number={}", latest_movie.movie_number)
            self._emit_progress(
                progress_callback,
                current=stats["processed_movies"],
                total=stats["candidate_movies"],
                text=f"回填描述失败 {latest_movie.movie_number}",
                summary_patch=stats,
            )
        return stats
