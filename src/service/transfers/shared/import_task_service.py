"""TaskRun-only boundary for provider-owned media imports."""

from __future__ import annotations

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.model import BackgroundTaskRun, DownloadTask, MediaLibrary
from src.model.base import get_database
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.schema.transfers.media_import import ImportAcceptedResponse, ImportRequest
from src.service.system import ActivityService
from src.service.transfers.downloads.common import download_provider, library_handle_for
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
    def execute(cls, reporter, params: dict) -> dict:
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
                progress_callback=reporter.progress_callback,
                stage_receipt_callback=lambda operation_key, receipt: cls._persist_stage_receipt(
                    task_run_id,
                    operation_key,
                    receipt,
                ),
                stage_receipt_commit_callback=lambda operation_key: cls._commit_stage_receipt(
                    task_run_id,
                    operation_key,
                ),
                stage_receipt_clear_callback=lambda operation_key: cls._clear_stage_receipt(
                    task_run_id,
                    operation_key,
                ),
                operation_namespace=f"task:{task_run_id}",
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
        if download_task_id is not None and result.new_playable_movies:
            try:
                create_new_media_reminder(
                    movie_items=result.new_playable_movies,
                    related_task_run_id=reporter.task_run_id,
                )
            except Exception as exc:
                logger.warning(
                    "Create import reminder skipped task_run_id={} detail={}",
                    reporter.task_run_id,
                    exc,
                )
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
    def _persist_stage_receipt(
        cls,
        task_run_id: int,
        operation_key: str,
        receipt: dict,
    ) -> None:
        cls._update_stage_receipt(
            task_run_id,
            operation_key,
            receipt=receipt,
            committed=False,
        )

    @classmethod
    def _commit_stage_receipt(cls, task_run_id: int, operation_key: str) -> None:
        cls._update_stage_receipt(
            task_run_id,
            operation_key,
            receipt=None,
            committed=True,
        )

    @classmethod
    def _clear_stage_receipt(cls, task_run_id: int, operation_key: str) -> None:
        with get_database().atomic():
            task_run = (
                BackgroundTaskRun.select()
                .where(BackgroundTaskRun.id == task_run_id)
                .for_update()
                .first()
            )
            if task_run is None:
                raise RuntimeError("import_task_run_not_found")
            params = dict(task_run.params or {})
            staged_receipts = dict(params.get("_staged_receipts") or {})
            staged_receipts.pop(operation_key, None)
            if staged_receipts:
                params["_staged_receipts"] = staged_receipts
            else:
                params.pop("_staged_receipts", None)
            task_run.params = params
            task_run.save(only=[BackgroundTaskRun.params])

    @staticmethod
    def _update_stage_receipt(
        task_run_id: int,
        operation_key: str,
        *,
        receipt: dict | None,
        committed: bool,
    ) -> None:
        """Persist provider receipts in the generic task params JSON.

        The receipt is written before the media transaction starts.  A failed
        finalize therefore remains recoverable without adding provider fields
        to a host model.
        """
        with get_database().atomic():
            task_run = (
                BackgroundTaskRun.select()
                .where(BackgroundTaskRun.id == task_run_id)
                .for_update()
                .first()
            )
            if task_run is None:
                raise RuntimeError("import_task_run_not_found")
            params = dict(task_run.params or {})
            staged_receipts = dict(params.get("_staged_receipts") or {})
            if receipt is None:
                current = staged_receipts.get(operation_key)
                if not isinstance(current, dict) or "receipt" not in current:
                    raise RuntimeError("import_stage_receipt_missing")
                staged_receipts[operation_key] = {
                    "receipt": current["receipt"],
                    "committed": committed,
                }
            else:
                staged_receipts[operation_key] = {
                    "receipt": receipt,
                    "committed": committed,
                }
            if staged_receipts:
                params["_staged_receipts"] = staged_receipts
            else:
                params.pop("_staged_receipts", None)
            task_run.params = params
            task_run.save(only=[BackgroundTaskRun.params])

    @classmethod
    def recover_interrupted_downloads(cls) -> int:
        failed_runs = list(BackgroundTaskRun.select().where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        ))
        recoverable_run_ids: list[int] = []
        for task_run in failed_runs:
            if cls._recover_staged_receipts(task_run):
                recoverable_run_ids.append(task_run.id)
        if not recoverable_run_ids:
            return 0
        return DownloadTask.update(import_status="pending").where(
            (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
            & DownloadTask.import_task_run.in_(recoverable_run_ids)
        ).execute()

    @classmethod
    def _recover_staged_receipts(cls, task_run: BackgroundTaskRun) -> bool:
        params = dict(task_run.params or {})
        staged_receipts = dict(params.get("_staged_receipts") or {})
        if not staged_receipts:
            return True
        try:
            request = ImportRequest.model_validate(params)
            library = MediaLibrary.get_by_id(request.library_id)
            storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
        except Exception as exc:
            logger.warning(
                "Import receipt recovery unavailable task_run_id={} detail={}",
                task_run.id,
                exc,
            )
            return False
        for operation_key, entry in staged_receipts.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("receipt"), dict):
                logger.error(
                    "Import receipt recovery found invalid entry task_run_id={} operation_key={}",
                    task_run.id,
                    operation_key,
                )
                return False
            try:
                if entry.get("committed"):
                    storage.finalize_import(receipt=entry["receipt"])
                else:
                    storage.abort_import(receipt=entry["receipt"])
            except Exception as exc:
                logger.warning(
                    "Import receipt recovery failed task_run_id={} operation_key={} detail={}",
                    task_run.id,
                    operation_key,
                    exc,
                )
                return False
            cls._clear_stage_receipt(task_run.id, operation_key)
        return True
