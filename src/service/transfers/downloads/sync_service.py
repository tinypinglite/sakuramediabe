"""Reconcile provider download snapshots into the host task ledger."""

from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import IMPORT_STATUS_PENDING, IMPORT_STATUS_RUNNING
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.plugins.provider_protocol import ProviderOperationError
from src.schema.transfers.downloads import DownloadClientSyncResponse
from src.service.transfers.downloads.common import (
    require_client,
    validate_remote_download_task,
)
from src.service.transfers.shared.import_task_service import ImportTaskService

# 幽灵任务清理的保守判据（模块常量，暂不开放配置）。
# 单次远端快照缺席不足以判定任务已删除：分页漂移、任务注册可见性延迟、与提交
# 落库的时序竞态都会造成假阴性。从未出现过的任务先给注册宽限；出现过的任务必须
# 连续缺席超过确认窗（同步每分钟一轮，5 分钟即连续 5 轮）才允许删除。
REMOTE_MISS_CONFIRM_SECONDS = 5 * 60
UNSEEN_TASK_GRACE_SECONDS = 15 * 60


class DownloadSyncService:
    _SYNCABLE_STATES = ("queued", "downloading")

    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory

    def _provider(self, client):
        if self.provider_factory is not None:
            return self.provider_factory(client)
        from src.service.transfers.downloads.common import download_provider

        return download_provider(client)

    def sync_client(self, client_id: int) -> DownloadClientSyncResponse:
        client = require_client(client_id)
        try:
            remote_tasks = tuple(self._provider(client).list_tasks())
        except ProviderOperationError as exc:
            logger.warning(
                "Download task sync provider error client_id={} code={} detail={}",
                client_id,
                exc.code,
                exc.safe_message,
            )
            raise self._provider_error(exc) from exc
        except Exception as exc:
            logger.exception("Download task sync failed client_id={} detail={}", client_id, exc)
            raise ApiError(
                502,
                "download_task_sync_failed",
                "下载提供方同步失败",
                {"client_id": client_id},
            ) from exc

        synced_at = utc_now_for_db()
        updated_count = unchanged_count = 0
        remote_ids: set[str] = set()
        for remote_task in remote_tasks:
            remote_task = validate_remote_download_task(remote_task)
            remote_ids.add(remote_task.remote_id)
            task = DownloadTask.get_or_none(
                (DownloadTask.client == client)
                & (DownloadTask.remote_id == remote_task.remote_id)
            )
            if task is None or task.movie is None:
                # 只跟踪宿主提交并已登记的任务，不把下载器里的外部任务带入自动导入链路。
                unchanged_count += 1
                continue
            changed: list = []
            for field, value in (
                (DownloadTask.name, remote_task.name),
                (DownloadTask.state, remote_task.state),
                (DownloadTask.progress, remote_task.progress),
                (DownloadTask.completed_source_ref, remote_task.completed_source_ref),
            ):
                if getattr(task, field.name) != value:
                    setattr(task, field.name, value)
                    changed.append(field)
            if changed:
                task.save(only=changed)
                updated_count += 1
            else:
                unchanged_count += 1

        self._mark_remote_seen(client.id, remote_ids, seen_at=synced_at)
        if remote_tasks:
            removed_count = self._prune_ghost_tasks(
                client.id, remote_ids, now=synced_at
            )
        else:
            removed_count = 0
            logger.warning(
                "Download provider returned an empty task snapshot; skip task pruning client_id={}",
                client.id,
            )
        return DownloadClientSyncResponse(
            client_id=client.id,
            scanned_count=len(remote_tasks),
            created_count=0,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            removed_count=removed_count,
        )

    @staticmethod
    def _mark_remote_seen(client_id: int, remote_ids: set[str], *, seen_at) -> None:
        """批量刷新本轮快照中出现的任务的 remote_seen_at。

        不走实例 save（避免逐任务写放大），也不触碰 updated_at——后者保留
        "最近一次字段变化"语义，remote_seen_at 才承担"最近一次远端可见"。
        """
        if not remote_ids:
            return
        DownloadTask.update(remote_seen_at=seen_at).where(
            (DownloadTask.client == client_id)
            & DownloadTask.remote_id.in_(tuple(sorted(remote_ids)))
        ).execute()

    @classmethod
    def _prune_ghost_tasks(
        cls, client_id: int, remote_ids: set[str], *, now=None
    ) -> int:
        """删除确认已消失的非终态台账行，判据必须保守。

        只清理仍与远端生命周期绑定的 queued / downloading 行：completed / failed
        是宿主持久台账，由用户手动删除终结，远端快照无权裁决；导入在途
        （import_status=running）的行绝不删除。单次远端快照缺席不算数（分页漂移、
        注册延迟、抓取时序竞态都可能造成假阴性）：从未出现过且已过注册宽限，或
        出现过但连续缺席超过确认窗，才允许删除。
        """
        if not remote_ids:
            return 0
        current = now or utc_now_for_db()
        confirm_cutoff = current - timedelta(seconds=REMOTE_MISS_CONFIRM_SECONDS)
        unseen_cutoff = current - timedelta(seconds=UNSEEN_TASK_GRACE_SECONDS)
        query = DownloadTask.delete().where(
            (DownloadTask.client == client_id)
            & DownloadTask.state.in_(cls._SYNCABLE_STATES)
            & (DownloadTask.import_status != IMPORT_STATUS_RUNNING)
            & (
                (
                    DownloadTask.remote_seen_at.is_null(True)
                    & (DownloadTask.created_at < unseen_cutoff)
                )
                | (
                    DownloadTask.remote_seen_at.is_null(False)
                    & (DownloadTask.remote_seen_at < confirm_cutoff)
                )
            )
        )
        query = query.where(DownloadTask.remote_id.not_in(list(remote_ids)))
        return query.execute()

    def sync_all_clients(self) -> dict[str, object]:
        summary = {
            "total_clients": 0,
            "scanned_count": 0,
            "created_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
            "removed_count": 0,
            "failed_count": 0,
            "failed_client_ids": [],
        }
        syncable_client_ids = {
            client_id
            for (client_id,) in DownloadTask.select(DownloadTask.client)
            .where(DownloadTask.state.in_(self._SYNCABLE_STATES))
            .distinct()
            .tuples()
        }
        clients = (
            list(
                DownloadClient.select()
                .where(DownloadClient.id.in_(tuple(sorted(syncable_client_ids))))
                .order_by(DownloadClient.id.asc())
            )
            if syncable_client_ids
            else []
        )
        logger.info("Download task sync started clients={}", len(clients))
        for client in clients:
            summary["total_clients"] += 1
            try:
                result = self.sync_client(client.id)
            except Exception as exc:
                logger.warning(
                    "Download task sync failed client_id={} detail={}", client.id, exc
                )
                summary["failed_count"] += 1
                summary["failed_client_ids"].append(client.id)
                continue
            logger.info(
                "Download task sync client finished client_id={} scanned={} updated={} removed={}",
                client.id,
                result.scanned_count,
                result.updated_count,
                result.removed_count,
            )
            for key in (
                "scanned_count",
                "created_count",
                "updated_count",
                "unchanged_count",
                "removed_count",
            ):
                summary[key] += getattr(result, key)
        return summary

    def enqueue_auto_imports(self) -> dict[str, int]:
        recovered_count = self._recover_orphaned_imports()
        queued_count = 0
        tasks_by_library: dict[int, list[DownloadTask]] = {}
        for task in DownloadTask.select().where(
            (DownloadTask.state == "completed")
            & (DownloadTask.completed_source_ref.is_null(False))
            & (DownloadTask.import_status == IMPORT_STATUS_PENDING)
            & (DownloadTask.movie.is_null(False))
        ).order_by(DownloadTask.id.asc()):
            tasks_by_library.setdefault(task.client.library_id, []).append(task)
        logger.info(
            "Download task auto import started pending_tasks={} libraries={} recovered={}",
            sum(len(tasks) for tasks in tasks_by_library.values()),
            len(tasks_by_library),
            recovered_count,
        )
        for library_id, tasks in tasks_by_library.items():
            try:
                ImportTaskService.enqueue_batch(tasks)
                queued_count += len(tasks)
                logger.info(
                    "Download task auto import enqueued library_id={} tasks={}",
                    library_id,
                    len(tasks),
                )
            except ApiError as exc:
                logger.warning(
                    "Skip auto import task_ids={} code={} detail={}",
                    [task.id for task in tasks],
                    exc.code,
                    exc.details,
                )
        return {"queued_count": queued_count, "recovered_count": recovered_count}

    def recover_orphaned_imports_only(self) -> dict[str, int]:
        return {"recovered_count": self._recover_orphaned_imports()}

    @staticmethod
    def _recover_orphaned_imports() -> int:
        return ImportTaskService.recover_interrupted_downloads()

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "task_not_managed": 409,
            "source_blacklisted": 422,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )
