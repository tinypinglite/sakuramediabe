import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from loguru import logger

from src.api.exception.errors import ApiError
from src.model import DownloadTask, ImportJob
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import DownloadTaskImportResponse, DownloadTaskResource
from src.service.transfers.common import (
    ALLOWED_DOWNLOAD_STATES,
    ALLOWED_IMPORT_STATUSES,
    build_task_movie_filter,
    normalize_state_filter,
    require_task,
    resolve_task_sort,
    validate_page,
    validate_task_ids,
)
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.service.transfers.media_import_service import MediaImportService


class DownloadTaskService:
    DEFAULT_IMPORTABLE_STATUSES = {"pending", "failed", "skipped"}

    @classmethod
    def list_tasks(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        client_id: Optional[int] = None,
        download_state: Optional[str] = None,
        import_status: Optional[str] = None,
        movie_number: Optional[str] = None,
        query: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> PageResponse[DownloadTaskResource]:
        validate_page(page, page_size)
        if client_id is not None and client_id <= 0:
            raise ApiError(
                422,
                "invalid_download_task_filter",
                "client_id must be greater than 0",
                {"client_id": client_id},
            )

        normalized_download_state = normalize_state_filter(
            download_state,
            field_name="download_state",
            allowed_values=ALLOWED_DOWNLOAD_STATES,
        )
        normalized_import_status = normalize_state_filter(
            import_status,
            field_name="import_status",
            allowed_values=ALLOWED_IMPORT_STATUSES,
        )
        order_by = resolve_task_sort(sort)

        base_query = DownloadTask.select()
        if client_id is not None:
            base_query = base_query.where(DownloadTask.client == client_id)
        if normalized_download_state is not None:
            base_query = base_query.where(DownloadTask.download_state == normalized_download_state)
        if normalized_import_status is not None:
            base_query = base_query.where(DownloadTask.import_status == normalized_import_status)
        if movie_number is not None and movie_number.strip():
            base_query = base_query.where(build_task_movie_filter(movie_number))
        if query is not None and query.strip():
            keyword = query.strip()
            base_query = base_query.where(
                (DownloadTask.name.contains(keyword))
                | (DownloadTask.info_hash.contains(keyword))
                | (DownloadTask.save_path.contains(keyword))
            )

        total = base_query.count()
        start = (page - 1) * page_size
        tasks = list(base_query.order_by(*order_by).offset(start).limit(page_size))
        return PageResponse[DownloadTaskResource](
            items=DownloadTaskResource.from_models(tasks),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def delete_tasks(task_ids: Optional[str]) -> None:
        if task_ids is None or not task_ids.strip():
            return
        DownloadTask.delete().where(DownloadTask.id.in_(validate_task_ids(task_ids))).execute()

    @classmethod
    def trigger_import(
        cls,
        task_id: int,
        *,
        allowed_statuses: Optional[Set[str]] = None,
    ) -> DownloadTaskImportResponse:
        task = require_task(task_id)
        if task.download_state != "completed":
            raise ApiError(
                422,
                "invalid_download_task_import",
                "Only completed download tasks can be imported",
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

        source_path = cls._resolve_import_source_path(task.save_path)
        import_job = ImportJob.create(
            source_path=str(source_path),
            library=task.client.media_library,
            download_task=task,
            state="pending",
        )
        task.import_status = "running"
        task.save()

        try:
            DownloadImportRunner.submit(import_job.id, cls._run_import_job, task.id, import_job.id)
        except Exception as exc:
            import_job.state = "failed"
            import_job.finished_at = datetime.utcnow()
            import_job.save()
            task.import_status = "failed"
            task.save()
            raise ApiError(
                502,
                "download_task_import_failed",
                "Failed to enqueue download task import",
                {"detail": str(exc), "task_id": task_id},
            ) from exc

        return DownloadTaskImportResponse(
            task_id=task.id,
            import_job_id=import_job.id,
            status="accepted",
        )

    @classmethod
    def _run_import_job(cls, task_id: int, import_job_id: int) -> None:
        ensure_database_ready()
        try:
            task = require_task(task_id)
            source_path = cls._resolve_import_source_path(task.save_path)
            service = MediaImportService()
            service.import_from_source(
                str(source_path),
                task.client.media_library_id,
                download_task_id=task.id,
                import_job_id=import_job_id,
            )
        except Exception as exc:
            cls._mark_import_failed(task_id, import_job_id, str(exc))
            logger.exception(
                "Download task import failed task_id={} import_job_id={}",
                task_id,
                import_job_id,
            )

    @staticmethod
    def _resolve_import_source_path(save_path: str) -> Path:
        path = Path(save_path).expanduser().resolve()
        if path.is_dir():
            return path
        if path.is_file():
            return path
        raise ApiError(
            422,
            "invalid_download_task_import_path",
            "Download task save path is not accessible",
            {"save_path": save_path},
        )

    @staticmethod
    def _mark_import_failed(task_id: int, import_job_id: int, detail: str) -> None:
        task = DownloadTask.get_or_none(DownloadTask.id == task_id)
        if task is not None:
            task.import_status = "failed"
            task.save()

        import_job = ImportJob.get_or_none(ImportJob.id == import_job_id)
        if import_job is None:
            return

        failure_items = []
        try:
            if import_job.failed_files:
                failure_items = json.loads(import_job.failed_files)
        except json.JSONDecodeError:
            failure_items = []

        failure_items.append(
            {
                "path": import_job.source_path,
                "reason": "import_job_bootstrap_failed",
                "detail": detail,
            }
        )
        import_job.failed_count = max(import_job.failed_count, 1)
        import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        import_job.state = "failed"
        import_job.finished_at = datetime.utcnow()
        import_job.save()
