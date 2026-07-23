from typing import Dict

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.process import is_process_alive
from src.common.movie_numbers import parse_movie_number_from_text
from src.model import DownloadClient, DownloadTask, ImportJob
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import DownloadClientSyncResponse
from src.service.system import ActivityService
from src.service.transfers.common import (
    ALLOWED_DOWNLOAD_STATES,
    DOWNLOAD_COMPLETE_STATES,
    map_download_state,
    map_remote_path,
    require_client,
)
from src.service.transfers.download_task_service import DownloadTaskService
from src.service.transfers.import_runner import DownloadImportRunner
from src.common.media_import_status import (
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError


class DownloadSyncService:
    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    @staticmethod
    def _has_live_owner_process(job: ImportJob) -> bool:
        task_run = job.task_run
        if task_run is None:
            return False
        return is_process_alive(task_run.owner_pid)

    def sync_client(self, client_id: int) -> DownloadClientSyncResponse:
        client = require_client(client_id)
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        try:
            remote_tasks = qb_client.list_torrents(client_id=client.id)
        except QBittorrentClientError as exc:
            # qBittorrent 客户端层本身不记日志，这里转 502 前先记，避免真实报错只存在于响应体
            logger.warning(
                "download task sync failed: client_id={} error={}",
                client_id,
                exc,
            )
            raise ApiError(
                502,
                "download_task_sync_failed",
                "qBittorrent request failed",
                {"detail": str(exc), "client_id": client_id},
            ) from exc

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        remote_hashes: set[str] = set()
        for remote_task in remote_tasks:
            normalized_state = map_download_state(remote_task.get("state", ""))
            movie_number = parse_movie_number_from_text(
                f"{remote_task.get('name', '')} {remote_task.get('save_path', '')}"
            ) or None
            mapped_path = map_remote_path(client, remote_task.get("save_path") or client.client_save_path)
            remote_hashes.add(remote_task["info_hash"])
            task, created = DownloadTask.get_or_create(
                client=client,
                info_hash=remote_task["info_hash"],
                defaults={
                    "movie": movie_number,
                    "name": remote_task.get("name", ""),
                    "save_path": mapped_path,
                    "progress": remote_task.get("progress", 0.0),
                    "download_state": normalized_state,
                    "import_status": IMPORT_STATUS_PENDING,
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

        removed_count = self._prune_ghost_tasks(client.id, remote_hashes)

        return DownloadClientSyncResponse(
            client_id=client.id,
            scanned_count=len(remote_tasks),
            created_count=created_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            removed_count=removed_count,
        )

    @staticmethod
    def _prune_ghost_tasks(client_id: int, remote_hashes: set[str]) -> int:
        """把 qB 侧已消失的下载任务从本地清掉，作为 sync 的反向对账。

        白名单（保留不删）：
        - import_status == running：runner 正在处理，删了会破坏 in-flight 状态
        - 有 state IN (pending, running) 的关联 ImportJob：导入队列还没消化完

        保底：qB 返回空列表时直接跳过，避免 tag 被误清 / 异常空返回一次抹掉所有本地记录。
        真需要清空的极端场景后续可加显式接口处理。ImportJob.download_task 是 SET NULL FK，
        清理 DownloadTask 不影响 ImportJob 里已导入的历史台账。

        用单条 DELETE 让白名单条件在 DB 层原子求值，关掉"先 SELECT ghost_ids、再按 id
        DELETE"中间被并发的 trigger_import 插队的 race——那种场景下正在启动导入的任务会被
        误删，runner 后续 require_task 会抛错。

        已知遗留：SSE hub 的 _task_index 有该 client 的 info_hash→task_id 内存索引，本函数
        不主动 evict。用户在活跃 SSE 会话期间恰好重新下载被 prune 掉的同一种子时，实时进度
        事件可能带旧 task_id，刷新页面即恢复。要闭环该窗口需要把 hub 引用穿透进本服务，暂
        不在本 PR 处理。
        """
        if not remote_hashes:
            logger.warning(
                "Skip ghost download task prune: qBittorrent returned empty list "
                "for client_id={} — check tag / auth state before manual cleanup",
                client_id,
            )
            return 0

        active_import_task_ids = ImportJob.select(ImportJob.download_task).where(
            ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING))
            & ImportJob.download_task.is_null(False)
        )
        removed_count = DownloadTask.delete().where(
            (DownloadTask.client == client_id)
            & DownloadTask.info_hash.not_in(list(remote_hashes))
            & (DownloadTask.import_status != IMPORT_STATUS_RUNNING)
            & DownloadTask.id.not_in(active_import_task_ids)
        ).execute()
        if removed_count:
            logger.info(
                "Pruned {} ghost download tasks for client_id={}",
                removed_count,
                client_id,
            )
        return removed_count

    def sync_all_clients(self) -> Dict[str, int]:
        total_scanned = 0
        total_created = 0
        total_updated = 0
        total_unchanged = 0
        total_removed = 0
        total_clients = 0
        failed_client_ids: list[int] = []
        # 本服务是 qb 专属对账；cloud115 离线任务由 Cloud115OfflineSyncService 独立对账。
        for client in (
            DownloadClient.select()
            .where(DownloadClient.kind == DownloadClientKind.QBITTORRENT.value)
            .order_by(DownloadClient.id.asc())
        ):
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
            total_removed += summary.removed_count
        return {
            "total_clients": total_clients,
            "scanned_count": total_scanned,
            "created_count": total_created,
            "updated_count": total_updated,
            "unchanged_count": total_unchanged,
            "removed_count": total_removed,
            "failed_count": len(failed_client_ids),
            "failed_client_ids": failed_client_ids,
        }

    def enqueue_auto_imports(self) -> Dict[str, int]:
        recovered_count = self._recover_orphaned_imports()
        queued_count = 0
        # 只排 qb 任务：cloud115 任务的落地是云端 cid，本地路径导入语义不适用，
        # 其完成后导入由 Cloud115OfflineSyncService 在对账时直接触发。
        qb_client_ids = DownloadClient.select(DownloadClient.id).where(
            DownloadClient.kind == DownloadClientKind.QBITTORRENT.value
        )
        for task in DownloadTask.select().where(
            DownloadTask.download_state.in_(DOWNLOAD_COMPLETE_STATES)
            & (DownloadTask.import_status == IMPORT_STATUS_PENDING)
            & DownloadTask.client.in_(qb_client_ids)
        ):
            try:
                DownloadTaskService.trigger_import(
                    task.id,
                    allowed_statuses={IMPORT_STATUS_PENDING},
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
        # qB 导入由本服务托管；115 导入状态由 Cloud115OfflineSyncService 独立对账，不能跨后端回收。
        running_qb_tasks = (
            DownloadTask.select(DownloadTask)
            .join(DownloadClient)
            .where(
                DownloadTask.import_status == IMPORT_STATUS_RUNNING,
                DownloadClient.kind == DownloadClientKind.QBITTORRENT.value,
            )
            .order_by(DownloadTask.id.asc())
        )
        for task in running_qb_tasks:
            running_jobs = list(
                ImportJob.select()
                .where(
                    (ImportJob.download_task == task.id)
                    & (ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
                )
                .order_by(ImportJob.id.asc())
            )
            if running_jobs and any(DownloadSyncService._has_live_owner_process(job) for job in running_jobs):
                continue
            if running_jobs and any(DownloadImportRunner.has_active_job(job.id) for job in running_jobs):
                continue

            for job in running_jobs:
                job.state = IMPORT_JOB_STATE_FAILED
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

            task.import_status = IMPORT_STATUS_PENDING
            task.save()
            recovered_count += 1
            logger.warning(
                "Recovered orphaned download import task_id={} import_job_ids={}",
                task.id,
                [job.id for job in running_jobs],
            )
        return recovered_count
