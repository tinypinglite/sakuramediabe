"""影片异步任务 service。

聚合翻译、互动同步、热度重算这几类需要通过 ``ActivityService.run_task`` 异步驱动的能力。
从 ``MovieService`` 拆出，避免主查询 service 承担过多副作用。
"""

from loguru import logger

from src.api.exception.errors import ApiError
from src.schema.catalog.movies import MovieDetailResource
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError
from src.service.catalog.movie_desc_translation_service import (
    MovieDescTranslationService,
    MovieDescTranslationTaskAbortError,
)
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_interaction_sync_service import MovieInteractionSyncService
from src.service.catalog.movie_service import MovieService
from src.service.system.activity_service import ActivityService


class MovieTaskService:
    """聚合影片相关的异步任务。"""

    @staticmethod
    def _build_movie_desc_translation_service() -> MovieDescTranslationService:
        return MovieDescTranslationService()

    @staticmethod
    def _build_movie_interaction_sync_service() -> MovieInteractionSyncService:
        return MovieInteractionSyncService()

    @classmethod
    def translate_movie_desc(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        translation_service = cls._build_movie_desc_translation_service()
        try:
            ActivityService.run_task(
                task_key=MovieDescTranslationService.TASK_KEY,
                trigger_type="manual",
                func=lambda _reporter: translation_service.translate_movie(movie),
            )
        except MovieDescTranslationClientError as exc:
            raise ApiError(
                exc.status_code,
                exc.error_code,
                exc.message,
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            ) from exc
        except MovieDescTranslationTaskAbortError as exc:
            raise ApiError(
                exc.status_code or 500,
                exc.error_code or "movie_desc_translation_failed",
                exc.message,
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": exc.message},
            ) from exc
        return MovieService.get_movie_detail(movie.movie_number)

    @classmethod
    def sync_movie_interactions(cls, movie_number: str) -> MovieDetailResource:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        if not str(movie.javdb_id or "").strip():
            raise ApiError(
                422,
                "movie_javdb_id_missing",
                "影片缺少 JavDB ID",
                {"movie_number": movie.movie_number, "movie_id": movie.id},
            )

        interaction_service = cls._build_movie_interaction_sync_service()
        try:
            ActivityService.run_task(
                task_key=MovieInteractionSyncService.TASK_KEY,
                trigger_type="manual",
                func=lambda _reporter: interaction_service.sync_movie(movie),
            )
        except Exception as exc:
            logger.exception(
                "Movie interaction sync failed movie_number={} detail={}",
                movie.movie_number,
                exc,
            )
            raise ApiError(
                502,
                "movie_interaction_sync_failed",
                "影片互动数同步失败",
                {"movie_number": movie.movie_number, "movie_id": movie.id, "detail": str(exc)},
            ) from exc
        return MovieService.get_movie_detail(movie.movie_number)

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
