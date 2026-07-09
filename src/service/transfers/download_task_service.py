import json
from pathlib import Path
from typing import Optional, Set

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, DownloadTask, ImportJob
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadTaskActionResponse,
    DownloadTaskImportResponse,
    DownloadTaskResource,
)
from src.service.system import ActivityService
from src.service.transfers.common import (
    build_task_movie_filter,
    require_task,
    require_client,
    resolve_task_sort,
    validate_page,
)
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.common.media_import_status import (
    FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
    make_failure_item,
)
from src.service.transfers.media_import_service import MediaImportService
from src.service.transfers.qbittorrent_client import (
    QBittorrentClient,
    QBittorrentClientError,
    QBittorrentTorrentNotFoundError,
    QBittorrentTorrentNotManagedError,
)


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
        sort: str | None = None,
    ) -> PageResponse[DownloadTaskResource]:
        validate_page(page, page_size)
        query = DownloadTask.select()
        if client_id is not None:
            require_client(client_id)
            query = query.where(DownloadTask.client == client_id)
        if movie_number and movie_number.strip():
            query = query.where(build_task_movie_filter(movie_number))

        total = query.count()
        tasks = list(
            query.order_by(*resolve_task_sort(sort)).paginate(page, page_size)
        )
        return PageResponse[DownloadTaskResource](
            items=DownloadTaskResource.from_models(tasks),
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def pause_task(
        cls,
        task_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadTaskActionResponse:
        task = require_task(task_id)
        cls._operate_remote_task(
            task,
            action="pause",
            qbittorrent_client_cls=qbittorrent_client_cls,
        )
        return DownloadTaskActionResponse(task_id=task.id, action="pause")

    @classmethod
    def resume_task(
        cls,
        task_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadTaskActionResponse:
        task = require_task(task_id)
        cls._operate_remote_task(
            task,
            action="resume",
            qbittorrent_client_cls=qbittorrent_client_cls,
        )
        return DownloadTaskActionResponse(task_id=task.id, action="resume")

    @classmethod
    def delete_task(
        cls,
        task_id: int,
        *,
        delete_files: bool,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> dict:
        task = require_task(task_id)
        if task.import_status == IMPORT_STATUS_RUNNING:
            raise ApiError(
                409,
                "download_task_import_running",
                "Cannot delete a download task while importing media",
                {"task_id": task.id},
            )

        qb_client = qbittorrent_client_cls.from_download_client(task.client)
        try:
            # 远端任务已被人工清理时，本地镜像仍可安全删除；若远端存在则客户端层会再次验标签。
            qb_client.delete_torrent(
                task.info_hash,
                client_id=task.client_id,
                delete_files=delete_files,
            )
        except QBittorrentTorrentNotManagedError as exc:
            raise ApiError(
                409,
                "download_task_not_managed",
                "qBittorrent torrent is not managed by this download client",
                {"task_id": task.id},
            ) from exc
        except QBittorrentClientError as exc:
            logger.warning("Delete qBittorrent task failed task_id={} detail={}", task.id, exc)
            raise ApiError(
                502,
                "download_task_delete_failed",
                "qBittorrent request failed",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc

        removed = {
            "task_id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "info_hash": task.info_hash,
        }
        task.delete_instance()
        return removed

    @classmethod
    def _operate_remote_task(cls, task: DownloadTask, *, action: str, qbittorrent_client_cls) -> None:
        qb_client = qbittorrent_client_cls.from_download_client(task.client)
        try:
            if action == "pause":
                qb_client.pause_torrent(task.info_hash, client_id=task.client_id)
            else:
                qb_client.resume_torrent(task.info_hash, client_id=task.client_id)
        except QBittorrentTorrentNotFoundError as exc:
            raise ApiError(
                409,
                "download_task_remote_missing",
                "qBittorrent torrent is no longer available",
                {"task_id": task.id},
            ) from exc
        except QBittorrentTorrentNotManagedError as exc:
            raise ApiError(
                409,
                "download_task_not_managed",
                "qBittorrent torrent is not managed by this download client",
                {"task_id": task.id},
            ) from exc
        except QBittorrentClientError as exc:
            logger.warning("{} qBittorrent task failed task_id={} detail={}", action, task.id, exc)
            raise ApiError(
                502,
                f"download_task_{action}_failed",
                "qBittorrent request failed",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc

    @classmethod
    def trigger_import(
        cls,
        task_id: int,
        *,
        allowed_statuses: Optional[Set[str]] = None,
        trigger_type: str = "manual",
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
            state=IMPORT_JOB_STATE_PENDING,
        )
        task_run = ActivityService.create_task_run(
            task_key="download_task_import",
            task_name=f"下载任务导入 {task.movie or task.name}",
            trigger_type=trigger_type,
        )
        # 导入作业必须持久关联 activity 任务，后续孤儿恢复才能精确回收状态。
        import_job.task_run = task_run
        import_job.save()
        task.import_status = IMPORT_STATUS_RUNNING
        task.save()

        try:
            DownloadImportRunner.submit(import_job.id, cls._run_import_job, task.id, import_job.id, task_run.id)
        except Exception as exc:
            import_job.state = IMPORT_JOB_STATE_FAILED
            import_job.finished_at = utc_now_for_db()
            import_job.save()
            task.import_status = IMPORT_STATUS_FAILED
            task.save()
            ActivityService.fail_task_run(
                task_run.id,
                error_message=str(exc),
                result_summary={
                    "task_id": task.id,
                    "import_job_id": import_job.id,
                },
            )
            raise ApiError(
                502,
                "download_task_import_failed",
                "Failed to enqueue download task import",
                {"detail": str(exc), "task_id": task_id},
            ) from exc

        return DownloadTaskImportResponse(
            task_id=task.id,
            import_job_id=import_job.id,
            task_run_id=task_run.id,
            status="accepted",
        )

    @classmethod
    def _run_import_job(
        cls,
        task_id: int,
        import_job_id: int,
        task_run_id: int | None = None,
    ) -> dict:
        ensure_database_ready()
        try:
            def _run_task(reporter):
                task = require_task(task_id)
                source_path = cls._resolve_import_source_path(task.save_path)
                service = MediaImportService()
                job = service.import_from_source(
                    str(source_path),
                    task.client.media_library_id,
                    download_task_id=task.id,
                    import_job_id=import_job_id,
                    progress_callback=reporter.progress_callback,
                )
                return {
                    "task_id": task.id,
                    "import_job_id": job.id,
                    "imported_count": job.imported_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "job_state": job.state,
                    "new_playable_movies": reporter.summary.get("new_playable_movies", []),
                }

            summary = ActivityService.run_task(
                task_key="download_task_import",
                task_name=None,
                trigger_type="internal",
                task_run_id=task_run_id,
                func=_run_task,
            )
        except Exception as exc:
            cls._mark_import_failed(task_id, import_job_id, str(exc))
            logger.exception(
                "Download task import failed task_id={} import_job_id={}",
                task_id,
                import_job_id,
            )
            return {
                "task_id": task_id,
                "import_job_id": import_job_id,
                "job_state": "failed",
            }
        # 通知链路不能反向影响导入主流程，因此提醒创建只做尽力而为。
        task_run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
        if task_run is not None and task_run.state == "completed":
            new_playable_movies = (task_run.result_summary or {}).get("new_playable_movies", [])
            if isinstance(new_playable_movies, list):
                try:
                    ActivityService.create_new_media_reminder(
                        movie_items=new_playable_movies,
                        related_task_run_id=task_run_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Create new media reminder skipped task_run_id={} detail={}",
                        task_run_id,
                        exc,
                    )
        return summary

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
            task.import_status = IMPORT_STATUS_FAILED
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
            make_failure_item(import_job.source_path, FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED, detail)
        )
        import_job.failed_count = max(import_job.failed_count, 1)
        import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        import_job.state = IMPORT_JOB_STATE_FAILED
        import_job.finished_at = utc_now_for_db()
        import_job.save()
