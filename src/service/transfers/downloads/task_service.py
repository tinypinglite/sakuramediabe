"""Download task ledger and host-side import handoff."""

from __future__ import annotations

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.common.service_helpers import validate_page, with_movie_card_relations
from src.model import DownloadTask, Movie
from src.plugins.provider_protocol import ProviderOperationError
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadTaskImportResponse,
    DownloadTaskResource,
)
from src.schema.transfers.media_import import ImportRequest
from src.service.transfers.downloads.common import (
    build_task_movie_filter,
    download_provider,
    is_download_complete,
    normalize_state_filters,
    require_task,
    resolve_task_sort,
)
from src.service.transfers.shared.import_task_service import ImportTaskService


class DownloadTaskService:
    DEFAULT_IMPORTABLE_STATUSES = {IMPORT_STATUS_PENDING, IMPORT_STATUS_FAILED, IMPORT_STATUS_SKIPPED}

    @classmethod
    def list_tasks(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        client_id: int | None = None,
        movie_number: str | None = None,
        state: list[str] | None = None,
        sort: str | None = None,
    ) -> PageResponse[DownloadTaskResource]:
        validate_page(page, page_size, error_code="invalid_download_task_filter")
        query = DownloadTask.select()
        if client_id is not None:
            query = query.where(DownloadTask.client == client_id)
        if movie_number and movie_number.strip():
            query = query.where(build_task_movie_filter(movie_number))
        normalized_states = normalize_state_filters(state, field_name="state")
        if normalized_states is not None:
            query = query.where(DownloadTask.state.in_(tuple(sorted(normalized_states))))
        total = query.count()
        tasks = list(query.order_by(*resolve_task_sort(sort)).paginate(page, page_size))
        movies_by_number = cls._load_movies_for_tasks(tasks)
        return PageResponse[DownloadTaskResource](
            items=DownloadTaskResource.from_models(tasks, movies_by_number=movies_by_number),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _load_movies_for_tasks(tasks) -> dict[str, Movie]:
        numbers = list({task.movie for task in tasks if task.movie})
        if not numbers:
            return {}
        movies, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies = movies.where(Movie.movie_number.in_(numbers))
        return {movie.movie_number: movie for movie in movies}

    @classmethod
    def delete_task(cls, task_id: int, *, delete_files: bool) -> dict:
        task = require_task(task_id)
        if task.import_status == IMPORT_STATUS_RUNNING:
            raise ApiError(
                409,
                "download_task_import_running",
                "Cannot delete a download task while importing media",
                {"task_id": task.id},
            )
        try:
            download_provider(task.client).delete_task(
                remote_id=task.remote_id,
                delete_files=delete_files,
            )
        except ProviderOperationError as exc:
            if exc.code != "source_not_found":
                raise cls._provider_error(exc) from exc
        removed = {
            "task_id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "remote_id": task.remote_id,
        }
        task.delete_instance()
        return removed

    @classmethod
    def trigger_import(
        cls,
        task_id: int,
        *,
        allowed_statuses: set[str] | None = None,
        trigger_type: str = "manual",
    ) -> DownloadTaskImportResponse:
        task = require_task(task_id)
        if not is_download_complete(task.state) or task.completed_source_ref is None:
            raise ApiError(
                422,
                "invalid_download_task_import",
                "Only completed download tasks with a source reference can be imported",
                {"task_id": task_id},
            )
        importable_statuses = allowed_statuses or cls.DEFAULT_IMPORTABLE_STATUSES
        if task.import_status not in importable_statuses:
            raise ApiError(
                409,
                "download_task_import_conflict",
                "Download task import is already running or completed",
                {"task_id": task_id, "import_status": task.import_status},
            )
        accepted = ImportTaskService.enqueue(
            ImportRequest(
                media_kind="jav" if task.movie else "video",
                library_id=task.client.library_id,
                source_ref=task.completed_source_ref,
                source_disposition="keep",
            ),
            trigger_type=trigger_type,
            download_task_id=task.id,
            task_name=f"下载任务导入 {task.movie or task.name}",
        )
        return DownloadTaskImportResponse(
            task_id=task.id,
            task_run_id=accepted.task_run_id,
            status="accepted",
        )

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "task_not_managed": 409,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )
