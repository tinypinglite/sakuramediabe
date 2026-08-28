"""Reconcile provider download snapshots into the host task ledger."""

from __future__ import annotations

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.model import DownloadClient, DownloadTask
from src.plugins.provider_protocol import ProviderOperationError
from src.schema.transfers.downloads import DownloadClientSyncResponse
from src.service.transfers.downloads.common import (
    require_client,
    validate_remote_download_task,
)
from src.service.transfers.downloads.task_service import DownloadTaskService
from src.service.transfers.shared.import_task_service import ImportTaskService


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
            raise self._provider_error(exc) from exc
        except Exception as exc:
            logger.exception("Download task sync failed client_id={} detail={}", client_id, exc)
            raise ApiError(
                502,
                "download_task_sync_failed",
                "下载提供方同步失败",
                {"client_id": client_id},
            ) from exc

        created_count = updated_count = unchanged_count = 0
        remote_ids: set[str] = set()
        for remote_task in remote_tasks:
            remote_task = validate_remote_download_task(remote_task)
            remote_ids.add(remote_task.remote_id)
            task, created = DownloadTask.get_or_create(
                client=client,
                remote_id=remote_task.remote_id,
                defaults={
                    "movie": None,
                    "name": remote_task.name,
                    "state": remote_task.state,
                    "progress": remote_task.progress,
                    "completed_source_ref": remote_task.completed_source_ref,
                    "import_status": IMPORT_STATUS_PENDING,
                },
            )
            if created:
                created_count += 1
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

        if remote_tasks:
            removed_count = self._prune_ghost_tasks(client.id, remote_ids)
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
    def _prune_ghost_tasks(client_id: int, remote_ids: set[str]) -> int:
        if not remote_ids:
            return 0
        importable_completed = (
            DownloadTask.completed_source_ref.is_null(False)
            & DownloadTask.import_status.in_(
                (IMPORT_STATUS_PENDING, IMPORT_STATUS_RUNNING, IMPORT_STATUS_FAILED)
            )
        )
        query = DownloadTask.delete().where(
            (DownloadTask.client == client_id)
            & (DownloadTask.import_status != IMPORT_STATUS_RUNNING)
            & ~importable_completed
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
        active_client_ids = (
            DownloadTask.select(DownloadTask.client)
            .where(DownloadTask.state.in_(self._SYNCABLE_STATES))
            .distinct()
        )
        for client in (
            DownloadClient.select()
            .where(DownloadClient.id.in_(active_client_ids))
            .order_by(DownloadClient.id.asc())
        ):
            summary["total_clients"] += 1
            try:
                result = self.sync_client(client.id)
            except Exception:
                summary["failed_count"] += 1
                summary["failed_client_ids"].append(client.id)
                continue
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
        for task in DownloadTask.select().where(
            (DownloadTask.state == "completed")
            & (DownloadTask.completed_source_ref.is_null(False))
            & (DownloadTask.import_status == IMPORT_STATUS_PENDING)
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
                    "Skip auto import task_id={} code={} detail={}",
                    task.id,
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
