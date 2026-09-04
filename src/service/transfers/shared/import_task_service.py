"""TaskRun-only boundary for provider-owned media imports."""

from __future__ import annotations

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.model import BackgroundTaskRun, DownloadTask, MediaLibrary
from src.model.base import get_database
from src.plugins.provider_protocol import (
    ProviderOperationError,
)
from src.schema.transfers.media_import import ImportAcceptedResponse, ImportRequest
from src.service.system import ActivityService
from src.service.transfers.downloads.common import download_provider
from src.service.transfers.shared.import_notifications import create_new_media_reminder
from src.service.transfers.shared.write_mutex import library_import_mutex_key


class ImportTaskService:
    TASK_KEY = "library_import"

    @classmethod
    def enqueue(
        cls,
        request: ImportRequest,
        *,
        trigger_type: str = "manual",
        download_task_id: int | None = None,
        task_name: str | None = None,
    ):
        request, library = cls._validated_request(request)
        mutex_key = library_import_mutex_key(library=library)
        params = request.model_dump()
        params["download_task_id"] = download_task_id
        try:
            with get_database().atomic():
                task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name=task_name or cls._task_name(request),
                    trigger_type=trigger_type,
                    mutex_key=mutex_key,
                    params=params,
                )
                if download_task_id is not None:
                    download_task = DownloadTask.get_by_id(download_task_id)
                    download_task.import_status = IMPORT_STATUS_RUNNING
                    download_task.import_task_run = task_run
                    download_task.save(
                        only=[DownloadTask.import_status, DownloadTask.import_task_run]
                    )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "import_task_conflict",
                "同一媒体库已有导入任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc
        except Exception as exc:
            if download_task_id is not None:
                DownloadTask.update(import_status=IMPORT_STATUS_FAILED).where(
                    DownloadTask.id == download_task_id
                ).execute()
            raise ApiError(
                502,
                "import_task_create_failed",
                "媒体导入任务入队失败",
                {"detail": str(exc)},
            ) from exc
        return ImportAcceptedResponse(
            task_run_id=task_run.id,
            task_key=task_run.task_key,
            state=task_run.state,
        )

    @classmethod
    def enqueue_batch(cls, download_tasks: list[DownloadTask]) -> None:
        if not download_tasks:
            raise ValueError("download_tasks must not be empty")
        library_ids = {task.client.library_id for task in download_tasks}
        if len(library_ids) != 1:
            raise ValueError("download_tasks must belong to one media library")
        library = MediaLibrary.get_by_id(next(iter(library_ids)))
        if library.provider_key == "":
            raise ApiError(422, "invalid_media_library_provider", "媒体库缺少 provider_key")
        batch_items = []
        for task in download_tasks:
            request = ImportRequest(
                media_kind="jav",
                library_id=library.id,
                source_ref=task.completed_source_ref,
                source_disposition="keep",
            )
            batch_items.append(
                {
                    "download_task_id": int(task.id),
                    **request.model_dump(),
                }
            )
        params = {
            "download_tasks": batch_items,
            "library_id": library.id,
        }
        mutex_key = library_import_mutex_key(library=library)
        try:
            with get_database().atomic():
                task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name=f"下载任务连续导入（{len(batch_items)}个）",
                    trigger_type="internal",
                    mutex_key=mutex_key,
                    params=params,
                )
                updated_count = (
                    DownloadTask.update(
                        import_status=IMPORT_STATUS_RUNNING,
                        import_task_run=task_run,
                    )
                    .where(
                        DownloadTask.id.in_([task.id for task in download_tasks]),
                        DownloadTask.import_status == IMPORT_STATUS_PENDING,
                    )
                    .execute()
                )
                if updated_count != len(download_tasks):
                    raise ApiError(
                        409,
                        "download_task_import_conflict",
                        "部分下载任务已被其它导入任务占用",
                    )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "import_task_conflict",
                "同一媒体库已有导入任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc

    @classmethod
    def execute(cls, reporter, params: dict) -> dict:
        if "download_tasks" in params:
            result = cls._execute_batch(reporter, params)
        else:
            result = cls._execute_single(
                reporter, params, progress_callback=reporter.progress_callback
            )
        if (
            "download_tasks" in params or params.get("download_task_id") is not None
        ) and result["new_playable_movies"]:
            try:
                create_new_media_reminder(
                    movie_items=result["new_playable_movies"],
                    related_task_run_id=reporter.task_run_id,
                )
            except Exception as exc:
                logger.warning(
                    "Create import reminder skipped task_run_id={} detail={}",
                    reporter.task_run_id,
                    exc,
                )
        return result

    @classmethod
    def _execute_batch(cls, reporter, params: dict) -> dict:
        batch_items = params["download_tasks"]
        if not batch_items:
            raise ValueError("download_tasks must not be empty")
        total = len(batch_items)
        processed_count = failed_task_count = 0
        new_playable_movies = []
        reporter.emit(current=0, total=total, text=f"待处理下载任务 {total} 个")
        for item in batch_items:
            task_id = int(item["download_task_id"])
            try:
                result = cls._execute_single(
                    reporter,
                    item,
                    progress_callback=None,
                    operation_namespace=f"task:{reporter.task_run_id}:download:{task_id}",
                )
                new_playable_movies.extend(result["new_playable_movies"])
                if result.get("failed_count", 0):
                    failed_task_count += 1
            except Exception:
                failed_task_count += 1
                logger.exception(
                    "Batch library import failed task_run_id={} download_task_id={}",
                    reporter.task_run_id,
                    task_id,
                )
            finally:
                processed_count += 1
                reporter.emit(
                    current=processed_count,
                    total=total,
                    text=f"已处理下载任务 {processed_count}/{total}",
                )
        return {
            "download_task_count": total,
            "processed_download_task_count": processed_count,
            "failed_download_task_count": failed_task_count,
            "new_playable_movies": new_playable_movies,
        }

    @classmethod
    def _execute_single(
        cls,
        reporter,
        params: dict,
        *,
        progress_callback,
        operation_namespace: str | None = None,
    ) -> dict:
        request = ImportRequest.model_validate(params)
        download_task_id = params.get("download_task_id")
        task_run_id = getattr(reporter, "task_run_id", None)
        if not isinstance(task_run_id, int):
            raise TypeError("import_task_run_id_missing")
        try:
            from src.service.transfers.imports.import_service import MediaImportService

            result = MediaImportService().import_from_source(
                request.source_ref,
                request.library_id,
                media_kind=request.media_kind,
                source_disposition=request.source_disposition,
                collection_id=request.collection_id,
                progress_callback=progress_callback,
                operation_namespace=operation_namespace or f"task:{task_run_id}",
            )
        except Exception:
            cls._set_download_status(download_task_id, IMPORT_STATUS_FAILED)
            raise
        if result.failed_count:
            status = IMPORT_STATUS_FAILED
        elif result.imported_count:
            status = IMPORT_STATUS_COMPLETED
        else:
            status = IMPORT_STATUS_SKIPPED
        cls._set_download_status(download_task_id, status)
        if status == IMPORT_STATUS_COMPLETED:
            cls._delete_remote_download_task(download_task_id)
        summary = result.model_dump()
        if download_task_id is not None:
            summary["download_task_id"] = int(download_task_id)
        return summary

    @staticmethod
    def _validated_request(request: ImportRequest) -> tuple[ImportRequest, MediaLibrary]:
        library = MediaLibrary.get_or_none(MediaLibrary.id == request.library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在")
        if library.provider_key == "":
            raise ApiError(422, "invalid_media_library_provider", "媒体库缺少 provider_key")
        return request, library

    @staticmethod
    def _task_name(request: ImportRequest) -> str:
        kind = "JAV" if request.media_kind == "jav" else "视频"
        return f"{kind}媒体库导入"

    @staticmethod
    def _set_download_status(download_task_id: int | None, status: str) -> None:
        if download_task_id is None:
            return
        DownloadTask.update(import_status=status).where(
            DownloadTask.id == int(download_task_id)
        ).execute()

    @staticmethod
    def _delete_remote_download_task(download_task_id: int | None) -> None:
        """Remove the provider task record after a successful import, preserving files."""
        if download_task_id is None:
            return
        task = DownloadTask.get_or_none(DownloadTask.id == int(download_task_id))
        if task is None:
            return
        try:
            download_provider(task.client).delete_task(
                remote_id=task.remote_id,
                delete_files=False,
            )
        except ProviderOperationError as exc:
            if exc.code == "source_not_found":
                return
            logger.warning(
                "Auto-delete remote download task failed task_id={} provider={} operation={} code={}",
                task.id,
                exc.provider_key,
                exc.operation,
                exc.code,
            )
        except ApiError as exc:
            logger.warning(
                "Auto-delete remote download task unavailable task_id={} code={}",
                task.id,
                exc.code,
            )
        except Exception as exc:
            logger.warning(
                "Auto-delete remote download task failed unexpectedly task_id={} error_type={}",
                task.id,
                type(exc).__name__,
            )

    @classmethod
    def recover_interrupted_downloads(cls) -> int:
        failed_runs = BackgroundTaskRun.select(BackgroundTaskRun.id).where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        )
        return DownloadTask.update(import_status=IMPORT_STATUS_PENDING).where(
            (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
            & DownloadTask.import_task_run.in_(failed_runs)
        ).execute()
