from typing import Dict

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.movie_numbers import parse_movie_number_from_text
from src.model import DownloadClient, DownloadTask, ImportJob
from src.schema.transfers.downloads import DownloadClientSyncResponse
from src.service.system import ActivityService
from src.service.transfers.common import (
    ALLOWED_DOWNLOAD_STATES,
    map_remote_path,
    require_client,
)
from src.service.transfers.download_task_service import DownloadTaskService
from src.service.transfers.import_runner import DownloadImportRunner
from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError


class DownloadSyncService:
    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    @staticmethod
    def _has_live_owner_process(job: ImportJob) -> bool:
        task_run = job.task_run
        if task_run is None:
            return False
        return ActivityService._is_process_alive(task_run.owner_pid)

    def sync_client(self, client_id: int) -> DownloadClientSyncResponse:
        client = require_client(client_id)
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        try:
            remote_tasks = qb_client.list_torrents(client_id=client.id)
        except QBittorrentClientError as exc:
            raise ApiError(
                502,
                "download_task_sync_failed",
                "qBittorrent request failed",
                {"detail": str(exc), "client_id": client_id},
            ) from exc

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        for remote_task in remote_tasks:
            normalized_state = self._map_download_state(
                remote_task.get("state", ""),
                progress=remote_task.get("progress", 0.0),
            )
            movie_number = parse_movie_number_from_text(
                f"{remote_task.get('name', '')} {remote_task.get('save_path', '')}"
            ) or None
            mapped_path = map_remote_path(client, remote_task.get("save_path") or client.client_save_path)
            task, created = DownloadTask.get_or_create(
                client=client,
                info_hash=remote_task["info_hash"],
                defaults={
                    "movie": movie_number,
                    "name": remote_task.get("name", ""),
                    "save_path": mapped_path,
                    "progress": remote_task.get("progress", 0.0),
                    "download_state": normalized_state,
                    "import_status": "pending",
                },
            )
            if created:
                created_count += 1
                continue

            changed = False
            if movie_number and task.movie != movie_number:
                task.movie = movie_number
                changed = True
            if task.name != remote_task.get("name", ""):
                task.name = remote_task.get("name", "")
                changed = True
            if task.save_path != mapped_path:
                task.save_path = mapped_path
                changed = True
            if task.progress != remote_task.get("progress", 0.0):
                task.progress = remote_task.get("progress", 0.0)
                changed = True
            if task.download_state != normalized_state:
                task.download_state = normalized_state
                changed = True
            if changed:
                task.save()
                updated_count += 1
            else:
                unchanged_count += 1

        return DownloadClientSyncResponse(
            client_id=client.id,
            scanned_count=len(remote_tasks),
            created_count=created_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
        )

    def sync_all_clients(self) -> Dict[str, int]:
        total_scanned = 0
        total_created = 0
        total_updated = 0
        total_unchanged = 0
        total_clients = 0
        failed_client_ids: list[int] = []
        for client in DownloadClient.select().order_by(DownloadClient.id.asc()):
            total_clients += 1
            try:
                summary = self.sync_client(client.id)
            except Exception as exc:
                failed_client_ids.append(client.id)
                logger.exception(
                    "Download task sync failed for client_id={} detail={}",
                    client.id,
                    exc,
                )
                continue
            total_scanned += summary.scanned_count
            total_created += summary.created_count
            total_updated += summary.updated_count
            total_unchanged += summary.unchanged_count
        return {
            "total_clients": total_clients,
            "scanned_count": total_scanned,
            "created_count": total_created,
            "updated_count": total_updated,
            "unchanged_count": total_unchanged,
            "failed_count": len(failed_client_ids),
            "failed_client_ids": failed_client_ids,
        }

    def enqueue_auto_imports(self) -> Dict[str, int]:
        recovered_count = self._recover_orphaned_imports()
        queued_count = 0
        for task in DownloadTask.select().where(
            (DownloadTask.download_state == "completed")
            & (DownloadTask.import_status == "pending")
        ):
            try:
                DownloadTaskService.trigger_import(
                    task.id,
                    allowed_statuses={"pending"},
                    trigger_type="internal",
                )
                queued_count += 1
            except ApiError as exc:
                logger.warning(
                    "Skip auto import for task_id={} code={} detail={}",
                    task.id,
                    exc.code,
                    exc.details,
                )
        return {"queued_count": queued_count, "recovered_count": recovered_count}

    def recover_orphaned_imports_only(self) -> Dict[str, int]:
        # 启动恢复场景只做状态回收，不触发新的自动导入排队。
        recovered_count = self._recover_orphaned_imports()
        return {"recovered_count": recovered_count}

    @staticmethod
    def _recover_orphaned_imports() -> int:
        recovered_count = 0
        for task in DownloadTask.select().where(DownloadTask.import_status == "running").order_by(DownloadTask.id.asc()):
            running_jobs = list(
                ImportJob.select()
                .where(
                    (ImportJob.download_task == task.id)
                    & (ImportJob.state.in_(("pending", "running")))
                )
                .order_by(ImportJob.id.asc())
            )
            if running_jobs and any(DownloadSyncService._has_live_owner_process(job) for job in running_jobs):
                continue
            if running_jobs and any(DownloadImportRunner.has_active_job(job.id) for job in running_jobs):
                continue

            for job in running_jobs:
                job.state = "failed"
                job.finished_at = utc_now_for_db()
                job.save()
                if job.task_run_id is not None:
                    allow_null_owner = bool(job.task_run is not None and job.task_run.trigger_type == "internal")
                    # 只有拿到持久 task_run_id，才回收对应 activity，避免靠名字或时间猜测。
                    ActivityService.recover_task_run(
                        job.task_run_id,
                        error_message="下载导入线程已中断，任务已失败",
                        result_summary={
                            "task_id": task.id,
                            "import_job_id": job.id,
                        },
                        allow_null_owner=allow_null_owner,
                    )

            task.import_status = "pending"
            task.save()
            recovered_count += 1
            logger.warning(
                "Recovered orphaned download import task_id={} import_job_ids={}",
                task.id,
                [job.id for job in running_jobs],
            )
        return recovered_count

    @staticmethod
    def _map_download_state(raw_state: str, *, progress: float) -> str:
        normalized = (raw_state or "").strip()
        if progress >= 1 or normalized in {"uploading", "stalledUP", "queuedUP", "pausedUP", "forcedUP"}:
            return "completed"
        if normalized in {"pausedDL", "pausedUP"}:
            return "paused"
        if normalized in {"error", "missingFiles"}:
            return "failed"
        if normalized in {"stalledDL", "stalledUP"}:
            return "stalled"
        if normalized in {"checkingDL", "checkingUP", "checkingResumeData"}:
            return "checking"
        if normalized in {"queuedDL", "queuedUP"}:
            return "queued"
        if normalized in {"downloading", "metaDL", "forcedDL", "allocating"}:
            return "downloading"
        if normalized.lower() in ALLOWED_DOWNLOAD_STATES:
            return normalized.lower()
        return "queued"
