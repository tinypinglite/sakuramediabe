"""TaskRun-only 的统一媒体导入边界。"""

from __future__ import annotations

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.fs_browse import assert_within_allowed_roots, normalize_abs_path
from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_RUNNING,
)
from src.config.config import settings
from src.model import DownloadTask, MediaLibrary
from src.model.base import get_database
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.media_import import (
    ImportAcceptedResponse,
    ImportRequest,
    ImportResult,
)
from src.service.system import ActivityService
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
    ) -> ImportAcceptedResponse:
        request, library = cls._validated_request(request)
        source = request.source_path or request.source_cid or request.source_fid or ""
        mutex_key = cls._mutex_key(request.backend, library)
        params = request.model_dump()
        params["download_task_id"] = download_task_id
        download_task = None
        try:
            # 在同一事务中创建队列行和关联下载记录，提交前 worker 不可见半成品。
            with get_database().atomic():
                task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name=task_name or cls._task_name(request, source),
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
                "同一导入互斥域已有任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc
        except Exception as exc:
            if download_task_id is not None:
                download_task = DownloadTask.get_or_none(DownloadTask.id == download_task_id)
            if download_task is not None:
                download_task.import_status = IMPORT_STATUS_FAILED
                download_task.save(only=[DownloadTask.import_status])
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
        try:
            result = cls._execute_request(
                request,
                progress_callback=reporter.progress_callback,
                managed_download_source=download_task_id is not None,
            )
        except Exception:
            cls._set_download_status(download_task_id, IMPORT_STATUS_FAILED)
            raise

        status = IMPORT_STATUS_FAILED if result.failed_count else IMPORT_STATUS_COMPLETED
        cls._set_download_status(download_task_id, status)
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
    def _execute_request(
        request: ImportRequest,
        *,
        progress_callback,
        managed_download_source: bool,
    ) -> ImportResult:
        if request.media_kind == "jav" and request.backend == "local":
            from src.service.transfers.imports.import_service import MediaImportService

            return MediaImportService().import_from_source(
                request.source_path or "",
                request.library_id,
                progress_callback=progress_callback,
                transfer_mode=request.transfer_mode or "auto",
            )
        if request.media_kind == "jav":
            from src.service.transfers.cloud115.importer.service import (
                Cloud115ImportService,
            )

            return Cloud115ImportService().import_from_cloud115(
                request.library_id,
                request.source_cid or "",
                progress_callback=progress_callback,
                managed_download_source=managed_download_source,
            )
        if request.backend == "local":
            from src.service.videos.video_import_service import VideoImportService

            return VideoImportService().import_from_source(
                request.source_path or "",
                request.library_id,
                transfer_mode=request.transfer_mode or "auto",
                collection_id=request.collection_id,
                progress_callback=progress_callback,
            )
        from src.service.videos.cloud115_video_import_service import (
            Cloud115VideoImportService,
        )

        return Cloud115VideoImportService().import_from_cloud115(
            request.library_id,
            source_cid=request.source_cid,
            source_fid=request.source_fid,
            collection_id=request.collection_id,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _validated_request(request: ImportRequest) -> tuple[ImportRequest, MediaLibrary]:
        library = MediaLibrary.get_or_none(MediaLibrary.id == request.library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在")
        expected = MediaLibraryBackend(request.backend).value
        if library.backend != expected:
            raise ApiError(422, "media_library_backend_mismatch", "媒体库后端与导入来源不匹配")
        if request.source_path is not None:
            source = normalize_abs_path(request.source_path)
            assert_within_allowed_roots(source, settings.media_import.browse_roots)
            request.source_path = str(source)
        if request.backend == "cloud115":
            request.transfer_mode = "cleanup-source"
        return request, library

    @classmethod
    def _mutex_key(
        cls,
        backend: str,
        library: MediaLibrary,
    ) -> str:
        """本地导入与 115 全局写入锁的统一入口。"""
        return library_import_mutex_key(backend=backend, library=library)

    @staticmethod
    def _task_name(request: ImportRequest, source: str) -> str:
        kind = "JAV" if request.media_kind == "jav" else "视频"
        backend = "115" if request.backend == "cloud115" else "本地"
        return f"{backend}{kind}导入 {source[-80:]}"

    @staticmethod
    def _set_download_status(download_task_id: int | None, status: str) -> None:
        if download_task_id is None:
            return
        DownloadTask.update(import_status=status).where(
            DownloadTask.id == int(download_task_id)
        ).execute()

    @classmethod
    def recover_interrupted_downloads(cls) -> int:
        """TaskRun 已失败时，仅把精确关联的下载任务恢复为可整源重试。"""
        from src.model import BackgroundTaskRun

        failed_runs = BackgroundTaskRun.select(BackgroundTaskRun.id).where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        )
        return (
            DownloadTask.update(import_status="pending")
            .where(
                (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
                & (DownloadTask.import_task_run.in_(failed_runs))
            )
            .execute()
        )
