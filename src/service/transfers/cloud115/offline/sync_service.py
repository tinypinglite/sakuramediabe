"""cloud115 离线任务对账：远端状态同步 + 完成自动导入 + 超时放弃。

职责（APS 周期任务，与 qb 的 DownloadSyncService 平行）：
1. 拉 115 离线任务列表，把进度/状态回写到本地 DownloadTask（只对账本系统提交的任务，
   即本地已有记录的 info_hash；用户自己在 115 加的离线任务不登记、不导入）。
2. 完成（status=2）且待导入的任务 → 触发 cloud115 导入（cleanup-source：把视频从缓冲区直接移动进库）。
3. 提交超过 ``downloads.cloud115_offline_abandon_hours``（默认 24h）仍未完成 → 本地标记
   ``abandoned`` + 发系统通知；不删 115 侧任务，后续对账不再关注（用户明确要求的语义）。
4. 同一媒体库的自动导入串行消费；每项成功后随机休息 10–30 秒再处理下一项。
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import rest_between_requests
from src.config.config import settings
from src.lib.cloud115 import Cloud115Error, OfflineTask
from src.model import DownloadClient, DownloadTask, ImportJob, get_database
from src.model.enums import DownloadClientKind
from src.service.cloud115 import cloud115_client_for
from src.service.transfers.cloud115.importer.common import Cloud115TargetDirCache
from src.service.transfers.cloud115.importer.job_service import Cloud115ImportJobService
from src.service.transfers.cloud115.offline.notifications import (
    create_cloud115_offline_abandoned_notification,
)
from src.service.transfers.cloud115.offline.service import (
    fetch_cloud115_offline_tasks_by_hash,
)
from src.service.transfers.shared.common import canonicalize_btih

# 115 离线任务 status → 本系统下载状态。-1=失败, 0=待办, 1=进行中, 2=完成。
CLOUD115_OFFLINE_STATE_MAP = {-1: "failed", 0: "queued", 1: "downloading", 2: "completed"}
# 本地放弃态：超时未完成后停止关注。不在 CLOUD115_OFFLINE_STATE_MAP 值域中，只由本服务写入。
DOWNLOAD_STATE_ABANDONED = "abandoned"


class Cloud115OfflineSyncService:
    # 远端列表分页拉取的安全上限：50/页 × 20 页 = 1000 条，远超单账号常态任务量。
    PAGE_SIZE = 50
    MAX_PAGES = 20
    IMPORT_POLL_INTERVAL_SECONDS = 1.0
    IMPORT_REST_MIN_SECONDS = 10.0
    IMPORT_REST_MAX_SECONDS = 30.0

    def run(self, progress_callback=None) -> dict:
        """APS 入口：对全部 cloud115 kind 的下载入口做一轮对账。"""
        stats = {
            "total_clients": 0,
            "recovered_import_count": self.recover_interrupted_imports(),
            "updated_count": 0,
            "import_triggered_count": 0,
            "abandoned_count": 0,
            "failed_client_ids": [],
        }
        clients = list(
            DownloadClient.select().where(
                DownloadClient.kind == DownloadClientKind.CLOUD115.value
            )
        )
        stats["total_clients"] = len(clients)
        for client in clients:
            try:
                summary = self.sync_client(client)
            except Exception as exc:
                stats["failed_client_ids"].append(client.id)
                logger.exception(
                    "cloud115 offline sync failed client_id={} detail={}", client.id, exc
                )
                continue
            stats["updated_count"] += summary["updated_count"]
            stats["import_triggered_count"] += summary["import_triggered_count"]
            stats["abandoned_count"] += summary["abandoned_count"]
        stats["failed_count"] = len(stats["failed_client_ids"])
        return stats

    def sync_client(self, client: DownloadClient) -> dict:
        summary = {"updated_count": 0, "import_triggered_count": 0, "abandoned_count": 0}
        tasks = list(DownloadTask.select().where(DownloadTask.client == client.id))

        # 先完成运行中作业的本地对账，再统一交给串行消费者选择下一项。
        for task in tasks:
            if (
                task.download_state == "completed"
                and task.import_status == IMPORT_STATUS_RUNNING
            ):
                self._reconcile_running_import(task)

        # 只有远端仍可能变化的两种状态才需要拉离线列表。
        active_tasks = {
            canonicalize_btih(task.info_hash): task
            for task in tasks
            if task.download_state in {"queued", "downloading"}
        }
        if active_tasks:
            remote_tasks = asyncio.run(self._fetch_remote_tasks(client))
            abandon_before = utc_now_for_db() - timedelta(
                hours=settings.downloads.cloud115_offline_abandon_hours
            )

            for info_hash, task in active_tasks.items():
                remote = remote_tasks.get(info_hash)
                if remote is not None and self._apply_remote_state(task, remote):
                    summary["updated_count"] += 1
                # 远端列表没有该 info_hash 时不动本地状态：分页上限内可能没拉全，宁可漏更新不误判。

                if task.download_state == "completed":
                    continue

                # 超时放弃：提交后 N 小时仍未完成。不清 115 任务，只停止本地关注 + 通知。
                if (
                    task.download_state in {"queued", "downloading"}
                    and task.created_at < abandon_before
                ):
                    self._abandon_task(task)
                    summary["abandoned_count"] += 1

        summary["import_triggered_count"] += self._drain_pending_imports(client)
        return summary

    @classmethod
    def _next_pending_import(cls, client_id: int) -> DownloadTask | None:
        return (
            DownloadTask.select()
            .where(
                (DownloadTask.client == client_id)
                & (DownloadTask.download_state == "completed")
                & (DownloadTask.import_status == IMPORT_STATUS_PENDING)
            )
            .order_by(DownloadTask.created_at.asc(), DownloadTask.id.asc())
            .first()
        )

    @classmethod
    def _drain_pending_imports(cls, client: DownloadClient) -> int:
        """同库单消费者串行导入；成功后随机休息，失败立即停止本轮。"""
        triggered_count = 0
        target_dir_cache = Cloud115TargetDirCache()

        while True:
            task = cls._next_pending_import(client.id)
            if task is None:
                return triggered_count

            response = cls._trigger_import(
                task,
                target_dir_cache=target_dir_cache,
            )
            if response is None:
                return triggered_count
            triggered_count += 1

            job = cls._wait_for_import_job(response.import_job_id)
            cls._reconcile_running_import(task)
            if (
                job is None
                or job.state != IMPORT_JOB_STATE_COMPLETED
                or job.failed_count > 0
            ):
                logger.warning(
                    "cloud115 import queue stopped after failed job client_id={} task_id={} job_id={}",
                    client.id,
                    task.id,
                    response.import_job_id,
                )
                return triggered_count

            if cls._next_pending_import(client.id) is None:
                return triggered_count
            delay = rest_between_requests(
                cls.IMPORT_REST_MIN_SECONDS,
                cls.IMPORT_REST_MAX_SECONDS,
            )
            logger.info(
                "cloud115 import queue resting client_id={} completed_task_id={} delay_seconds={:.1f}",
                client.id,
                task.id,
                delay,
            )
            time.sleep(delay)

    @classmethod
    def _wait_for_import_job(cls, import_job_id: int) -> ImportJob | None:
        """只轮询本地作业状态，不访问 115。"""
        while True:
            job = ImportJob.get_or_none(ImportJob.id == import_job_id)
            if job is None or job.state in (
                IMPORT_JOB_STATE_COMPLETED,
                IMPORT_JOB_STATE_FAILED,
            ):
                return job
            time.sleep(cls.IMPORT_POLL_INTERVAL_SECONDS)

    async def _fetch_remote_tasks(self, client: DownloadClient) -> dict[str, OfflineTask]:
        """分页拉全量离线任务，按 info_hash 索引。"""
        async with cloud115_client_for(client.media_library) as sdk_client:
            return await fetch_cloud115_offline_tasks_by_hash(
                sdk_client,
                page_size=self.PAGE_SIZE,
                max_pages=self.MAX_PAGES,
            )

    @classmethod
    def recover_interrupted_imports(cls) -> int:
        """删除 Cloud115 半截 ImportJob，并把下载任务恢复为待导入。

        BackgroundTaskRun 作为审计记录保留；条件删除/更新使 API 与 APS 重复执行安全。
        """
        tasks = list(
            DownloadTask.select(DownloadTask)
            .join(DownloadClient)
            .where(
                (DownloadClient.kind == DownloadClientKind.CLOUD115.value)
                & (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
            )
        )
        recovered_count = 0
        for task in tasks:
            unfinished_jobs = list(
                ImportJob.select()
                .where(
                    (ImportJob.download_task == task.id)
                    & (ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
                )
                .order_by(ImportJob.id.asc())
            )
            if any(cls._import_job_is_alive(job) for job in unfinished_jobs):
                continue

            # 有终态作业时交给正常对账回写，不能误重置后再次导入。
            if not unfinished_jobs and ImportJob.select().where(
                ImportJob.download_task == task.id
            ).exists():
                continue

            with get_database().atomic():
                candidate_still_exists = False
                if unfinished_jobs:
                    job_ids = [job.id for job in unfinished_jobs]
                    ImportJob.delete().where(
                        (ImportJob.id.in_(job_ids))
                        & (ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
                    ).execute()
                    # runner 若在条件删除前刚写入终态，该作业会保留下来，不能把 task 误重置为 pending。
                    candidate_still_exists = ImportJob.select().where(
                        ImportJob.id.in_(job_ids)
                    ).exists()
                remaining_unfinished = ImportJob.select().where(
                    (ImportJob.download_task == task.id)
                    & (ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
                ).exists()
                updated = 0
                if not candidate_still_exists and not remaining_unfinished:
                    updated = (
                        DownloadTask.update(import_status=IMPORT_STATUS_PENDING)
                        .where(
                            (DownloadTask.id == task.id)
                            & (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
                        )
                        .execute()
                    )
            if updated:
                recovered_count += 1
                logger.info("recovered interrupted cloud115 import task_id={}", task.id)
        return recovered_count

    @staticmethod
    def _import_job_is_alive(job: ImportJob) -> bool:
        # 队列托管后判活看 task_run：排队中或租约未过期即视为仍在执行。
        from src.service.transfers.shared.base_import_job_service import BaseImportJobService

        return BaseImportJobService._task_run_alive(job)

    @staticmethod
    def _apply_remote_state(task: DownloadTask, remote: OfflineTask) -> bool:
        """把远端进度/状态写回本地任务，返回是否发生变化。"""
        changed = False
        normalized_state = CLOUD115_OFFLINE_STATE_MAP.get(remote.status)
        if normalized_state and task.download_state != normalized_state:
            task.download_state = normalized_state
            changed = True
        progress = round(remote.percent_done / 100.0, 4)
        if task.progress != progress:
            task.progress = progress
            changed = True
        if remote.name and task.name != remote.name:
            task.name = remote.name
            changed = True
        if changed:
            task.save()
        return changed

    @classmethod
    def trigger_task_import(
        cls,
        task: DownloadTask,
        *,
        target_dir_cache: Cloud115TargetDirCache | None = None,
    ):
        """触发 cleanup-source 导入并把 ImportJob 关联回任务，返回 ImportJobTriggerResponse。

        供两处复用：本服务对账时的自动触发（吞冲突留待下轮）与任务中心的手动导入
        （异常原样抛给 API 层）。
        """
        target_ref = task.target_ref or {}
        source_cid = target_ref.get("cid")
        if not source_cid:
            raise ApiError(
                422,
                "invalid_download_task_import_path",
                "cloud115 下载任务缺少落地目录 cid，无法导入",
                {"task_id": task.id},
            )
        response = Cloud115ImportJobService.trigger_cloud115_import(
            task.client.media_library_id,
            source_cid,
            transfer_mode="cleanup-source",
            managed_download_source=True,
            target_dir_cache=target_dir_cache,
            task_name=(
                f"{Cloud115ImportJobService.TRIGGER_TASK_NAME_PREFIX} "
                f"{task.movie or task.name}"
            ),
        )
        # 关联 ImportJob ← DownloadTask：后续状态对账与孤儿恢复都靠这条链。
        ImportJob.update(download_task=task.id).where(
            ImportJob.id == response.import_job_id
        ).execute()
        task.import_status = IMPORT_STATUS_RUNNING
        task.save()
        return response

    @classmethod
    def _trigger_import(
        cls,
        task: DownloadTask,
        *,
        target_dir_cache: Cloud115TargetDirCache,
    ):
        """对账场景的自动触发：触发失败留待后续轮次处理。"""
        try:
            return cls.trigger_task_import(
                task,
                target_dir_cache=target_dir_cache,
            )
        except ApiError as exc:
            if exc.code == "invalid_download_task_import_path":
                # 缺 target_ref.cid 属数据缺陷，重试不可恢复：标失败停止自动重试。
                logger.error(
                    "cloud115 offline task missing target_ref.cid task_id={}", task.id
                )
                task.import_status = IMPORT_STATUS_FAILED
                task.save()
            else:
                logger.warning(
                    "cloud115 offline import trigger failed task_id={} code={} detail={}",
                    task.id, exc.code, exc.details,
                )
            return None
        except Cloud115Error as exc:
            logger.warning(
                "cloud115 offline import trigger failed task_id={} detail={}", task.id, exc
            )
            return None

    @staticmethod
    def _reconcile_running_import(task: DownloadTask) -> None:
        """导入中的任务按最新关联 ImportJob 的终态回写 import_status。"""
        job = (
            ImportJob.select()
            .where(ImportJob.download_task == task.id)
            .order_by(ImportJob.id.desc())
            .first()
        )
        if job is None:
            return
        if job.state == IMPORT_JOB_STATE_COMPLETED:
            task.import_status = (
                IMPORT_STATUS_FAILED if job.failed_count > 0 else IMPORT_STATUS_COMPLETED
            )
            task.save()
        elif job.state == IMPORT_JOB_STATE_FAILED:
            task.import_status = IMPORT_STATUS_FAILED
            task.save()

    @staticmethod
    def _abandon_task(task: DownloadTask) -> None:
        task.download_state = DOWNLOAD_STATE_ABANDONED
        task.save()
        logger.warning(
            "cloud115 offline task abandoned after timeout task_id={} info_hash={}",
            task.id, task.info_hash,
        )
        # 通知失败不影响对账主流程。
        try:
            create_cloud115_offline_abandoned_notification(
                task_name=task.movie or task.name,
                task_id=task.id,
            )
        except Exception as exc:
            logger.warning(
                "cloud115 offline abandon notification skipped task_id={} detail={}",
                task.id, exc,
            )
