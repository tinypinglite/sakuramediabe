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
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import rest_between_requests
from src.config.config import settings
from src.lib.cloud115 import Cloud115Error, OfflineTask
from src.model import BackgroundTaskRun, DownloadClient, DownloadTask
from src.model.enums import DownloadClientKind
from src.schema.transfers.media_import import ImportRequest
from src.service.cloud115 import cloud115_client_for
from src.service.transfers.cloud115.offline.notifications import (
    create_cloud115_offline_abandoned_notification,
)
from src.service.transfers.cloud115.offline.service import (
    fetch_cloud115_offline_tasks_by_hash,
)
from src.service.transfers.shared.common import canonicalize_btih
from src.service.transfers.shared.import_task_service import ImportTaskService

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
        while True:
            task = cls._next_pending_import(client.id)
            if task is None:
                return triggered_count

            response = cls._trigger_import(task)
            if response is None:
                return triggered_count
            triggered_count += 1

            task_run = cls._wait_for_import_task(response.task_run_id)
            cls._reconcile_running_import(task)
            if (
                task_run is None
                or task_run.state != "completed"
                or int((task_run.result_summary or {}).get("failed_count") or 0) > 0
            ):
                logger.warning(
                    "cloud115 import queue stopped after failed task client_id={} task_id={} task_run_id={}",
                    client.id,
                    task.id,
                    response.task_run_id,
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
    def _wait_for_import_task(cls, task_run_id: int) -> BackgroundTaskRun | None:
        """只轮询统一 TaskRun，不访问 115。"""
        while True:
            task_run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
            if task_run is None or task_run.state in ("completed", "failed"):
                return task_run
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
        return ImportTaskService.recover_interrupted_downloads()

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
        trigger_type: str = "scheduled",
    ):
        """触发固定 move 的统一导入任务。"""
        target_ref = task.target_ref or {}
        source_cid = target_ref.get("cid")
        if not source_cid:
            raise ApiError(
                422,
                "invalid_download_task_import_path",
                "cloud115 下载任务缺少落地目录 cid，无法导入",
                {"task_id": task.id},
            )
        accepted = ImportTaskService.enqueue(
            ImportRequest(
                media_kind="jav",
                backend="cloud115",
                library_id=task.client.media_library_id,
                source_cid=source_cid,
            ),
            trigger_type=trigger_type,
            download_task_id=task.id,
            task_name=f"115 下载任务导入 {task.movie or task.name}",
        )
        from src.schema.transfers.downloads import DownloadTaskImportResponse

        return DownloadTaskImportResponse(
            task_id=task.id,
            task_run_id=accepted.task_run_id,
            status="accepted",
        )

    @classmethod
    def _trigger_import(
        cls,
        task: DownloadTask,
    ):
        """对账场景的自动触发：触发失败留待后续轮次处理。"""
        try:
            return cls.trigger_task_import(task)
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
        """导入中的任务按关联 TaskRun 终态与摘要回写 import_status。"""
        task_run = BackgroundTaskRun.get_or_none(
            BackgroundTaskRun.id == task.import_task_run_id
        )
        if task_run is None:
            return
        if task_run.state == "completed":
            task.import_status = (
                IMPORT_STATUS_FAILED
                if int((task_run.result_summary or {}).get("failed_count") or 0) > 0
                else IMPORT_STATUS_COMPLETED
            )
            task.save()
        elif task_run.state == "failed":
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
