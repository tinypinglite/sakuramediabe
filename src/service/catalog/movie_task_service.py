"""影片同步任务 service。"""

from src.api.exception.errors import ApiError
from src.schema.catalog.movies import MovieHeatRecomputeParams
from src.schema.system.jobs import ManualJobTriggerResponse
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_service import MovieService
from src.service.system.activity_service import TaskRunConflictError


class MovieTaskService:
    """聚合影片相关的异步任务入口。"""

    @classmethod
    def recompute_movie_heat(cls, movie_number: str) -> ManualJobTriggerResponse:
        movie, _ = MovieService.require_movie_by_normalized_number(movie_number)
        try:
            from src.scheduler.registry import JOB_REGISTRY_BY_KEY
            from src.start.aps import submit_manual_job

            task_run = submit_manual_job(
                JOB_REGISTRY_BY_KEY["movie_heat_update"],
                params={"movie_number": movie.movie_number},
            )
        except TaskRunConflictError as exc:
            blocking = exc.blocking_task_run
            raise ApiError(
                409,
                "movie_heat_recompute_conflict",
                "影片热度任务正在执行",
                {"blocking_task_run_id": blocking.id},
            ) from exc
        return ManualJobTriggerResponse(
            task_run_id=task_run.id,
            task_key=task_run.task_key,
            state=task_run.state,
        )

    @staticmethod
    def execute_movie_heat(_reporter, params: dict) -> dict[str, int | str]:
        payload = MovieHeatRecomputeParams.model_validate(params)
        movie, _ = MovieService.require_movie_by_normalized_number(payload.movie_number)
        return {
            "movie_id": movie.id,
            "movie_number": movie.movie_number,
            "updated_count": MovieHeatService.update_single_movie_heat(movie.id),
            "formula_version": MovieHeatService.FORMULA_VERSION,
        }
