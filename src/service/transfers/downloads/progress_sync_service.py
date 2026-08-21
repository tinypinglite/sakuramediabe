"""qBittorrent 下载进度数据库快照同步。"""

from __future__ import annotations

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.model.enums import DownloadClientKind
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.downloads.common import (
    QB_ETA_INFINITY,
    resolve_qbittorrent_download_state,
)


class DownloadProgressSyncService:
    """只刷新已有 qB 下载任务的进度快照，不承担任务全量对账职责。"""

    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    def sync_client(self, client_id: int) -> dict[str, int]:
        client = DownloadClient.get_or_none(DownloadClient.id == client_id)
        if client is None:
            raise ApiError(
                404,
                "download_client_not_found",
                "Download client not found",
                {"client_id": client_id},
            )
        # cloud115 使用独立离线任务对账；这里绝不借 qB 客户端采样。
        if client.kind != DownloadClientKind.QBITTORRENT.value:
            return self._empty_client_summary(client.id)

        # 采样只服务本地已登记任务；没有任务时不触发 qB Web API 请求。
        if not DownloadTask.select(DownloadTask.id).where(
            DownloadTask.client == client.id
        ).exists():
            return self._empty_client_summary(client.id)

        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        try:
            try:
                remote_tasks = qb_client.list_torrents(client_id=client.id)
            except QBittorrentClientError as exc:
                # 请求失败时不写任何默认值，保留上一次完整快照。
                raise ApiError(
                    502,
                    "download_progress_sync_failed",
                    "qBittorrent request failed",
                    {"detail": str(exc), "client_id": client.id},
                ) from exc

            return self._persist_client_snapshot(client, remote_tasks)
        finally:
            # 采样周期短，必须每轮主动释放连接池，不能等待垃圾回收。
            qb_client.close()

    def _persist_client_snapshot(
        self,
        client: DownloadClient,
        remote_tasks: list[dict],
    ) -> dict[str, int]:
        """把一次完整远端响应写成已有任务的数据库快照。"""

        remote_by_hash = {
            str(remote.get("info_hash") or "").lower(): remote
            for remote in remote_tasks
            if str(remote.get("info_hash") or "").strip()
        }
        if not remote_by_hash:
            return {
                "client_id": client.id,
                "scanned_count": 0,
                "updated_count": 0,
                "unchanged_count": 0,
            }

        # 仅选取本地已登记行：采样从不创建、删除或触发导入，任务生命周期仍归全量对账负责。
        tasks = DownloadTask.select().where(
            (DownloadTask.client == client.id)
            & (DownloadTask.info_hash.in_(tuple(remote_by_hash)))
        )
        now = utc_now_for_db()
        updated_count = 0
        unchanged_count = 0
        for task in tasks:
            remote = remote_by_hash[task.info_hash]
            snapshot = self._build_snapshot(remote)
            changed_fields = [
                field
                for field, value in snapshot.items()
                if getattr(task, field.name) != value
            ]
            if not changed_fields:
                unchanged_count += 1
                continue

            for field, value in snapshot.items():
                setattr(task, field.name, value)
            # 只有远端指标真正变化时才推进同步时刻，避免静态种子每轮产生无意义写入。
            task.progress_synced_at = now
            task.save(only=[*changed_fields, DownloadTask.progress_synced_at])
            updated_count += 1

        return {
            "client_id": client.id,
            "scanned_count": len(remote_by_hash),
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
        }

    @staticmethod
    def _empty_client_summary(client_id: int) -> dict[str, int]:
        return {
            "client_id": client_id,
            "scanned_count": 0,
            "updated_count": 0,
            "unchanged_count": 0,
        }

    def sync_all_clients(self) -> dict[str, int | list[int]]:
        """逐个采样 qB 客户端；单客户端失败不影响其余客户端的旧快照。"""
        total_scanned = 0
        total_updated = 0
        total_unchanged = 0
        failed_client_ids: list[int] = []
        clients = list(
            DownloadClient.select().where(
                DownloadClient.kind == DownloadClientKind.QBITTORRENT.value
            )
        )
        for client in clients:
            try:
                summary = self.sync_client(client.id)
            except QBittorrentClientError as exc:
                # 构造客户端阶段的外部错误尚未转换成 ApiError，仍按单客户端故障隔离。
                failed_client_ids.append(client.id)
                logger.warning(
                    "Download progress snapshot sync failed: client_id={} error={}",
                    client.id,
                    exc,
                )
                continue
            except ApiError as exc:
                # 外部下载器单点故障允许隔离；数据库与未知程序错误必须向上抛，交任务框架判失败。
                failed_client_ids.append(client.id)
                logger.warning(
                    "Download progress snapshot sync failed: client_id={} code={} details={}",
                    client.id,
                    exc.code,
                    exc.details,
                )
                continue
            total_scanned += summary["scanned_count"]
            total_updated += summary["updated_count"]
            total_unchanged += summary["unchanged_count"]
        return {
            "total_clients": len(clients),
            "scanned_count": total_scanned,
            "updated_count": total_updated,
            "unchanged_count": total_unchanged,
            "failed_count": len(failed_client_ids),
            "failed_client_ids": failed_client_ids,
        }

    @staticmethod
    def _build_snapshot(remote: dict) -> dict:
        raw_state = str(remote.get("state") or "")
        eta_seconds = DownloadProgressSyncService._as_optional_int(remote.get("eta"))
        if eta_seconds is not None and eta_seconds >= QB_ETA_INFINITY:
            eta_seconds = None
        return {
            DownloadTask.progress: DownloadProgressSyncService._as_float(remote.get("progress")),
            DownloadTask.download_state: resolve_qbittorrent_download_state(
                raw_state, remote.get("last_activity")
            ),
            DownloadTask.raw_state: raw_state,
            DownloadTask.download_speed_bytes: DownloadProgressSyncService._as_int(
                remote.get("dlspeed")
            ),
            DownloadTask.uploaded_speed_bytes: DownloadProgressSyncService._as_int(
                remote.get("upspeed")
            ),
            DownloadTask.downloaded_bytes: DownloadProgressSyncService._as_int(
                remote.get("downloaded")
            ),
            DownloadTask.total_size_bytes: DownloadProgressSyncService._as_int(
                remote.get("size") or remote.get("total_size")
            ),
            DownloadTask.eta_seconds: eta_seconds,
        }

    @staticmethod
    def _as_int(value: object) -> int:
        return int(value or 0)

    @staticmethod
    def _as_float(value: object) -> float:
        return float(value or 0.0)

    @staticmethod
    def _as_optional_int(value: object) -> int | None:
        if value is None:
            return None
        return int(value)
