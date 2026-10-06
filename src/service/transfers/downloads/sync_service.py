"""Reconcile provider download snapshots into the host task ledger."""

from __future__ import annotations

import re
from datetime import timedelta

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadSubmissionRecord, DownloadTask
from src.plugins.provider_protocol import ProviderOperationError, RemoteDownloadTask
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
# 重认领回溯窗：最近这段时间内"有宿主提交记录、但任务行缺失"的下载器会被重新
# 纳入同步，覆盖"本地台账被清空后该下载器退出同步、自愈无从执行"的缺口。
ORPHAN_ADOPTION_LOOKBACK_HOURS = 24
_INFO_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


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
        updated_count = unchanged_count = created_count = 0
        remote_ids: set[str] = set()
        for remote_task in remote_tasks:
            remote_task = validate_remote_download_task(remote_task)
            remote_ids.add(remote_task.remote_id)
            task = DownloadTask.get_or_none(
                (DownloadTask.client == client)
                & (DownloadTask.remote_id == remote_task.remote_id)
            )
            if task is None:
                task = self._adopt_submitted_task(client, remote_task)
                if task is None:
                    # 只跟踪宿主提交并已登记的任务，不把下载器里的外部任务带入自动导入链路。
                    unchanged_count += 1
                    continue
                created_count += 1
                continue
            if task.movie is None:
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
            created_count=created_count,
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
    def _adopt_submitted_task(
        cls, client: DownloadClient, remote_task: RemoteDownloadTask
    ) -> DownloadTask | None:
        """远端仍有任务、本地台账缺失时，按提交历史重建台账行。

        只认领有成功提交记录（state=submitted）且 remote_id / info_hash 能对上的
        任务，因此不会把用户在下载器里自行添加的外部任务带进自动导入链路。竞态或
        快照漏检导致的误删会在下一轮同步立即恢复跟踪，而不是等到下一次订阅搜索。
        """
        remote_id = (remote_task.remote_id or "").strip()
        if not remote_id:
            return None
        record = cls._latest_submitted_record(client.id, remote_id)
        if record is None:
            return None
        task, created = DownloadTask.get_or_create(
            client=client,
            remote_id=remote_id,
            defaults={
                "movie": record.movie_number,
                "name": remote_task.name or f"{record.movie_number}-{remote_id[:6]}",
                "state": remote_task.state,
                "progress": remote_task.progress,
                "completed_source_ref": remote_task.completed_source_ref,
                "import_status": IMPORT_STATUS_PENDING,
            },
        )
        logger.info(
            "Download task re-adopted from submission record client_id={} "
            "remote_id={} movie_number={} created={}",
            client.id,
            remote_id,
            record.movie_number,
            created,
        )
        cls._rebind_submission_records(client.id, remote_id, task.id)
        return task

    @staticmethod
    def _submitted_remote_match(remote_id: str):
        match = DownloadSubmissionRecord.remote_id == remote_id
        if _INFO_HASH_PATTERN.fullmatch(remote_id):
            match = match | (DownloadSubmissionRecord.info_hash == remote_id.lower())
        return match

    @classmethod
    def _latest_submitted_record(cls, client_id: int, remote_id: str):
        return (
            DownloadSubmissionRecord.select()
            .where(
                (DownloadSubmissionRecord.client_id == client_id)
                & (DownloadSubmissionRecord.state == "submitted")
                & cls._submitted_remote_match(remote_id)
            )
            .order_by(DownloadSubmissionRecord.id.desc())
            .first()
        )

    @classmethod
    def _rebind_submission_records(
        cls, client_id: int, remote_id: str, task_id: int
    ) -> None:
        """把悬挂的 submitted 记录一次性指回重建的任务行。

        重复提交命中同一远端任务会产生多条记录；只修最新一条会让旧记录一直悬
        空，孤儿下载器扫描持续命中、白白多同步。只修补 task_id 为空或已失效的
        记录，不动仍指向其他活跃任务的同 hash 记录。批量 update 需手动带上
        updated_at（实例 save 的自动推进不适用于 Model.update）。
        """
        dangling = DownloadSubmissionRecord.task_id.is_null(True) | (
            DownloadSubmissionRecord.task_id.not_in(
                DownloadTask.select(DownloadTask.id)
            )
        )
        DownloadSubmissionRecord.update(
            task_id=task_id, updated_at=utc_now_for_db()
        ).where(
            (DownloadSubmissionRecord.client_id == client_id)
            & (DownloadSubmissionRecord.state == "submitted")
            & dangling
            & cls._submitted_remote_match(remote_id)
        ).execute()

    @classmethod
    def _prune_ghost_tasks(
        cls, client_id: int, remote_ids: set[str], *, now=None
    ) -> int:
        """删除确认已消失的本地台账行，判据必须保守。

        单次远端快照缺席不算数（分页漂移、注册延迟、抓取时序竞态都可能造成假
        阴性）：从未出现过且已过注册宽限，或出现过但连续缺席超过确认窗，才允许
        删除；导入在途与可重试（失败/跳过）的完成态都受保护。
        """
        if not remote_ids:
            return 0
        current = now or utc_now_for_db()
        confirm_cutoff = current - timedelta(seconds=REMOTE_MISS_CONFIRM_SECONDS)
        unseen_cutoff = current - timedelta(seconds=UNSEEN_TASK_GRACE_SECONDS)
        importable_completed = (
            DownloadTask.completed_source_ref.is_null(False)
            & DownloadTask.import_status.in_(
                (
                    IMPORT_STATUS_PENDING,
                    IMPORT_STATUS_RUNNING,
                    IMPORT_STATUS_FAILED,
                    IMPORT_STATUS_SKIPPED,
                )
            )
        )
        query = DownloadTask.delete().where(
            (DownloadTask.client == client_id)
            & (DownloadTask.import_status != IMPORT_STATUS_RUNNING)
            & ~importable_completed
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

    @staticmethod
    def _recently_orphaned_client_ids() -> set[int]:
        """最近有宿主提交记录、但任务行已缺失的下载器。

        同步的客户端集合原本只包含仍持有活跃任务的下载器；一旦某个下载器的本地
        台账被误删清空，它就永久退出同步，重认领永远没有机会执行。这里按近期的
        孤儿提交记录把这类下载器补回来。宿主主动删除会留下 deleted 墓碑，不会命中。
        """
        cutoff = utc_now_for_db() - timedelta(hours=ORPHAN_ADOPTION_LOOKBACK_HOURS)
        rows = (
            DownloadSubmissionRecord.select(DownloadSubmissionRecord.client_id)
            .left_outer_join(
                DownloadTask,
                on=(DownloadSubmissionRecord.task_id == DownloadTask.id),
            )
            .where(
                (DownloadSubmissionRecord.state == "submitted")
                & DownloadTask.id.is_null(True)
                & (DownloadSubmissionRecord.updated_at >= cutoff)
            )
            .distinct()
            .tuples()
        )
        return {int(client_id) for (client_id,) in rows}

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
        syncable_client_ids |= self._recently_orphaned_client_ids()
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
