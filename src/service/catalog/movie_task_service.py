"""影片异步任务 service（任务架构 Wave 3：单影片任务入队 worker 执行）。

翻译与互动同步曾在 HTTP 请求线程同步执行（无 mutex、长任务占请求线程），
现改为 202 入队：worker 按 params.movie_id 走单资源 kernel 记账路径执行，
前端经 task_run SSE 事件跟进完成后刷新详情。热度重算是毫秒级纯 SQL，保持同步。
"""

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import Movie
from src.schema.catalog.movies import MovieDetailResource, MovieTaskQueuedResponse
from src.service.catalog.movie_desc_translation_service import MovieDescTranslationService
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_interaction_sync_service import MovieInteractionSyncService
from src.service.catalog.movie_service import MovieService
from src.service.system.activity_service import ActivityService


class MovieTaskService:
    """聚合影片相关的异步任务。"""

    @classmethod
    def _enqueue_single_movie_task(
        cls, *, task_key: str, movie: Movie, task_name: str
    ) -> MovieTaskQueuedResponse:
        try:
            task_run = ActivityService.create_task_run(
                task_key=task_key,
                task_name=task_name,
                trigger_type="manual",
                # 影片粒度互斥：同影片同任务连点去重；与全量批跑（aps:*）不抢锁，
                # 资源级并发由 kernel 候选查询排除 running 行兜底。
                mutex_key=f"movie_task:{task_key}:{movie.id}",
                params={"movie_id": movie.id},
                scheduled_at=utc_now_for_db(),
            )
        except IntegrityError as exc:
            raise ApiError(
                409,
                "movie_task_conflict",
                "该影片的同类任务已在排队或执行中",
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            ) from exc
        return MovieTaskQueuedResponse(
            movie_id=movie.id,
            movie_number=movie.movie_number,
            task_key=task_key,
            task_run_id=task_run.id,
        )

    @classmethod
    def translate_movie_desc(cls, movie_number: str) -> MovieTaskQueuedResponse:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        if not str(movie.desc or "").strip():
            raise ApiError(
                422,
                "movie_desc_missing",
                "影片缺少原始简介",
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            )
        return cls._enqueue_single_movie_task(
            task_key=MovieDescTranslationService.TASK_KEY,
            movie=movie,
            task_name=f"影片简介翻译 {movie.movie_number}",
        )

    @classmethod
    def sync_movie_interactions(cls, movie_number: str) -> MovieTaskQueuedResponse:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        if not str(movie.javdb_id or "").strip():
            raise ApiError(
                422,
                "movie_javdb_id_missing",
                "影片缺少 JavDB ID",
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            )
        return cls._enqueue_single_movie_task(
            task_key=MovieInteractionSyncService.TASK_KEY,
            movie=movie,
            task_name=f"影片互动数同步 {movie.movie_number}",
        )

    @classmethod
    def recompute_movie_heat(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        try:
            ActivityService.run_task(
                task_key="movie_heat_update",
                trigger_type="manual",
                func=lambda _reporter: {
                    # 单影片热度重算沿用现有公式，并把结果写入活动中心汇总。
                    "movie_id": movie.id,
                    "movie_number": movie.movie_number,
                    "updated_count": MovieHeatService.update_single_movie_heat(movie.id),
                    "formula_version": MovieHeatService.FORMULA_VERSION,
                },
            )
        except Exception as exc:
            logger.exception(
                "Movie heat recompute failed movie_number={} detail={}",
                movie.movie_number,
                exc,
            )
            raise ApiError(
                500,
                "movie_heat_recompute_failed",
                "影片热度重算失败",
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": str(exc)},
            ) from exc
        return MovieService.get_movie_detail(movie.movie_number)
