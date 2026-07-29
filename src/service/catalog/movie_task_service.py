"""影片同步小任务 service。

单影片翻译 / 互动同步的入队入口已并入统一 action 协议
（POST /system/resource-task-actions 的 rerun + only_ids），本类只剩热度重算：
毫秒级纯 SQL，保持同步响应。
"""

from loguru import logger

from src.api.exception.errors import ApiError
from src.schema.catalog.movies import MovieDetailResource
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_service import MovieService
from src.service.system.activity_service import ActivityService


class MovieTaskService:
    """聚合影片相关的同步小任务。"""

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
