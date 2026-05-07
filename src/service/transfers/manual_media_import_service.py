from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from peewee import JOIN, IntegrityError

from src.api.exception.errors import ApiError
from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import validate_page
from src.model import BackgroundTaskRun, ImportJob, MediaLibrary
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import ManualMediaImportResponse, MediaImportHistoryResource
from src.service.system import ActivityService, TaskRunItemService
from src.service.system.background_task_runner import BackgroundTaskRunner
from src.service.system.system_directory_service import SystemDirectoryService
from src.service.transfers.media_import_service import MediaImportCanceled, MediaImportService


class ManualMediaImportService:
    """前端手动触发的已有媒体导入编排服务。"""

    TASK_KEY = "manual_media_import"
    TASK_NAME = "手动媒体导入"
    MUTEX_KEY = "manual_media_import"
    ITEM_TYPE = "media_import_file"

    @classmethod
    def _active_import_exists(cls) -> bool:
        return BackgroundTaskRun.select().where(
            BackgroundTaskRun.task_key == cls.TASK_KEY,
            BackgroundTaskRun.state.in_(("pending", "running")),
        ).exists()

    @staticmethod
    def _require_library(library_id: int) -> MediaLibrary:
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在", {"library_id": library_id})
        return library

    @classmethod
    def submit_import(cls, *, library_id: int, source_path: str) -> ManualMediaImportResponse:
        library = cls._require_library(library_id)
        source = SystemDirectoryService.resolve_safe_path(source_path)
        if not source.exists() or (not source.is_dir() and not source.is_file()):
            raise ApiError(422, "source_path_not_found", "导入源路径不存在", {"source_path": str(source)})
        if cls._active_import_exists():
            raise ApiError(409, "manual_media_import_conflict", "已有媒体导入任务正在运行")

        try:
            task_run = ActivityService.create_task_run(
                task_key=cls.TASK_KEY,
                task_name=cls.TASK_NAME,
                trigger_type="manual",
                mutex_key=cls.MUTEX_KEY,
            )
        except IntegrityError as exc:
            # mutex_key 唯一索引兜住并发提交，避免竞态泄漏数据库异常。
            raise ApiError(409, "manual_media_import_conflict", "已有媒体导入任务正在运行") from exc
        import_job = ImportJob.create(
            source_path=str(source),
            library=library,
            state="pending",
            task_run=task_run,
        )
        try:
            BackgroundTaskRunner.submit(task_run.id, cls._run_import_job, import_job.id, task_run.id)
        except Exception as exc:
            import_job.state = "failed"
            import_job.finished_at = utc_now_for_db()
            import_job.save()
            ActivityService.fail_task_run(
                task_run.id,
                error_message=str(exc),
                result_summary={"import_job_id": import_job.id},
            )
            raise ApiError(502, "manual_media_import_enqueue_failed", "媒体导入任务入队失败", {"detail": str(exc)}) from exc
        return ManualMediaImportResponse(task_run_id=task_run.id, import_job_id=import_job.id, status="accepted")

    @classmethod
    def _record_item(cls, task_run_id: int, payload: dict[str, Any]) -> None:
        event = str(payload.get("event") or "")
        source_path = str(payload.get("source_path") or payload.get("item_key") or "")
        if not source_path:
            return
        state_map = {
            "item_pending": "pending",
            "item_running": "running",
            "item_succeeded": "succeeded",
            "item_skipped": "skipped",
            "item_failed": "failed",
        }
        state = state_map.get(event)
        if state is None:
            return
        movie_id = payload.get("movie_id")
        media_id = payload.get("media_id")
        extra = {
            key: payload.get(key)
            for key in (
                "movie_number",
                "storage_mode",
                "content_fingerprint",
                "skip_reason",
            )
            if payload.get(key) is not None
        }
        TaskRunItemService.upsert_item(
            task_run_id=task_run_id,
            item_type=cls.ITEM_TYPE,
            item_key=source_path,
            state=state,
            source_path=source_path,
            target_path=payload.get("target_path"),
            resource_type="media" if media_id is not None else None,
            resource_id=int(media_id) if media_id is not None else None,
            related_resource_type="movie" if movie_id is not None else None,
            related_resource_id=int(movie_id) if movie_id is not None else None,
            error_code=payload.get("error_code"),
            error_detail=payload.get("error_detail"),
            extra_patch=extra,
        )

    @classmethod
    def _run_import_job(cls, import_job_id: int, task_run_id: int) -> dict:
        ensure_database_ready()
        import_job = ImportJob.get_by_id(import_job_id)

        def _run_task(reporter):
            service = MediaImportService()
            job = service.import_from_source(
                import_job.source_path,
                import_job.library_id,
                import_job_id=import_job.id,
                progress_callback=reporter.progress_callback,
                item_callback=lambda payload: cls._record_item(task_run_id, payload),
                cancel_checker=lambda: ActivityService.is_task_cancel_requested(task_run_id),
            )
            return {
                "import_job_id": job.id,
                "library_id": job.library_id,
                "source_path": job.source_path,
                "imported_count": job.imported_count,
                "skipped_count": job.skipped_count,
                "failed_count": job.failed_count,
                "job_state": job.state,
                "task_run_state": "completed_with_errors" if job.failed_count > 0 else "completed",
                "new_playable_movies": reporter.summary.get("new_playable_movies", []),
            }

        try:
            result = ActivityService.run_task(
                task_key=cls.TASK_KEY,
                task_name=cls.TASK_NAME,
                trigger_type="manual",
                task_run_id=task_run_id,
                func=_run_task,
            )
            return result
        except MediaImportCanceled:
            TaskRunItemService.mark_canceled_pending_items(task_run_id, item_type=cls.ITEM_TYPE)
            import_job = ImportJob.get_by_id(import_job_id)
            result = {
                "import_job_id": import_job.id,
                "library_id": import_job.library_id,
                "source_path": import_job.source_path,
                "imported_count": import_job.imported_count,
                "skipped_count": import_job.skipped_count,
                "failed_count": import_job.failed_count,
                "job_state": "canceled",
            }
            ActivityService.cancel_task_run(task_run_id, result_summary=result)
            return result
        except Exception as exc:
            logger.exception("Manual media import failed import_job_id={} task_run_id={}", import_job_id, task_run_id)
            import_job = ImportJob.get_or_none(ImportJob.id == import_job_id)
            if import_job is not None and import_job.state in {"pending", "running"}:
                import_job.state = "failed"
                import_job.finished_at = utc_now_for_db()
                import_job.save()
            if BackgroundTaskRun.get_by_id(task_run_id).state in {"pending", "running"}:
                ActivityService.fail_task_run(
                    task_run_id,
                    error_message=str(exc),
                    result_summary={"import_job_id": import_job_id},
                )
            return {"import_job_id": import_job_id, "job_state": "failed"}

    @classmethod
    def list_import_history(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        library_id: int | None = None,
        source_path: str | None = None,
        state: str | None = None,
    ) -> PageResponse[MediaImportHistoryResource]:
        validate_page(page, page_size, error_code="invalid_media_import_history_filter")
        query = (
            ImportJob.select(ImportJob, BackgroundTaskRun)
            .join(BackgroundTaskRun, JOIN.LEFT_OUTER, on=(ImportJob.task_run == BackgroundTaskRun.id))
            .where((BackgroundTaskRun.task_key == cls.TASK_KEY) | (ImportJob.task_run.is_null(True)))
        )
        if library_id is not None:
            query = query.where(ImportJob.library == library_id)
        if source_path:
            query = query.where(ImportJob.source_path.contains(source_path.strip()))
        if state:
            query = query.where(ImportJob.state == state.strip().lower())
        query = query.order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
        total = query.count()
        items = []
        for job in query.offset((page - 1) * page_size).limit(page_size):
            task_run = job.task_run if job.task_run_id is not None else None
            items.append(
                MediaImportHistoryResource(
                    import_job_id=job.id,
                    task_run_id=job.task_run_id,
                    library_id=job.library_id,
                    source_path=job.source_path,
                    state=job.state,
                    imported_count=job.imported_count,
                    skipped_count=job.skipped_count,
                    failed_count=job.failed_count,
                    task_state=task_run.state if task_run is not None else None,
                    progress_current=task_run.progress_current if task_run is not None else None,
                    progress_total=task_run.progress_total if task_run is not None else None,
                    progress_text=task_run.progress_text if task_run is not None else None,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                )
            )
        return PageResponse[MediaImportHistoryResource](items=items, page=page, page_size=page_size, total=total)

    @classmethod
    def recover_orphaned_imports(cls) -> dict[str, int]:
        recovered_count = 0
        query = ImportJob.select().where(ImportJob.state.in_(("pending", "running")), ImportJob.task_run.is_null(False))
        for job in query:
            task_run = job.task_run
            if task_run.task_key != cls.TASK_KEY:
                continue
            if BackgroundTaskRunner.has_active_task(task_run.id):
                continue
            job.state = "failed"
            job.finished_at = utc_now_for_db()
            job.save()
            ActivityService.recover_task_run(
                task_run.id,
                error_message="手动媒体导入线程已中断，任务已失败",
                result_summary={"import_job_id": job.id},
                allow_null_owner=True,
                force=True,
            )
            recovered_count += 1
        return {"recovered_count": recovered_count}
