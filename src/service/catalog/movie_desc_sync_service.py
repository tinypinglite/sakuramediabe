from __future__ import annotations

from src.model import Movie
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.system.resource_task_runner import (
    ResourceTaskLedger,
    ResourceTaskRunner,
    ResourceTaskSpec,
)


class MovieDescSyncService:
    """影片描述回填（已迁 kernel 记账，任务架构 Wave 2）。

    候选 = 描述为空 ∩ 内核状态条件（pending / 到期的 failed_retryable）；
    DMM 熔断经 ``ensure_dmm_available_or_abort`` 显式中止整轮（剩余资源不耗预算），
    取代旧的"静默 return False 不写状态"。
    """

    TASK_KEY = "movie_desc_sync"
    INTERRUPTED_FETCH_ERROR_MESSAGE = "影片描述抓取任务中断，等待重试"

    def __init__(self, catalog_import_service: CatalogImportService | None = None):
        self.catalog_import_service = catalog_import_service or CatalogImportService()

    @classmethod
    def _select_candidates(cls, state_condition, only_ids=None):
        # 只取 id：完整模型由内核按 CANDIDATE_BATCH_SIZE 分批加载，避免全量驻留内存。
        query = (
            Movie.select(Movie.id)
            .where(state_condition(cls.TASK_KEY, "movie", Movie.id))
            # 已订阅影片优先（订阅时间升序），其余按 id 稳定排序。
            .order_by(Movie.subscribed_at.is_null(), Movie.subscribed_at.asc(), Movie.id.asc())
        )
        if only_ids:
            # 手动指定即强制（统一 action 的 rerun 语义）：已有简介也重拉覆盖。
            query = query.where(Movie.id.in_(list(only_ids)))
        else:
            # 批跑只补缺。
            query = query.where(Movie.desc == "")
        return query

    def _process_one(self, _ctx, movie: Movie) -> None:
        service = self.catalog_import_service
        service.ensure_dmm_available_or_abort()
        movie_desc = service.fetch_movie_desc_strict(movie)
        service._apply_movie_desc(movie, movie_desc)

    @classmethod
    def recover_interrupted_running_movies(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_FETCH_ERROR_MESSAGE
        return ResourceTaskLedger.recover_running(cls.TASK_KEY, error_message=normalized_error)

    def run(self, *, reporter, only_ids: list[int] | None = None) -> dict[str, int]:
        spec = ResourceTaskSpec(
            task_key=self.TASK_KEY,
            resource_type="movie",
            retry=CatalogImportService.DESC_SYNC_RETRY_POLICY,
            select_candidates=self._select_candidates,
            process_one=self._process_one,
            resource_model=Movie,
        )
        stats = ResourceTaskRunner.run(spec, reporter, only_ids=only_ids)
        # 保持旧统计口径（registry format_stats 与日志锚点不变）。
        return {
            "candidate_movies": stats["candidate_count"],
            "processed_movies": stats["candidate_count"] - stats["deferred_count"],
            "succeeded_movies": stats["succeeded_count"],
            "failed_movies": (
                stats["failed_retryable_count"]
                + stats["failed_terminal_count"]
                + stats["exhausted_count"]
            ),
            "updated_movies": stats["succeeded_count"],
            "skipped_movies": 0,
        }
