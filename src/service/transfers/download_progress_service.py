"""下载进度 SSE 的进程内轮询与订阅管理（qBittorrent Sync delta + cloud115 离线列表轮询）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, RLock, Thread
from typing import Dict, Optional

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.database import ensure_database_ready
from src.config.config import settings
from src.lib.cloud115 import Cloud115Error, OfflineTask
from src.model import DownloadClient, DownloadTask
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import (
    DownloadClientTransferResource,
    DownloadTaskProgressResource,
)
from src.service.transfers.cloud115_offline_sync_service import (
    CLOUD115_OFFLINE_STATE_MAP,
    Cloud115OfflineSyncService,
)
from src.service.transfers.common import (
    QB_ETA_INFINITY,
    build_task_movie_filter,
    is_qb_managed_torrent,
    resolve_qbittorrent_download_state,
)
from src.service.transfers.qbittorrent_client import QBittorrentClient, QBittorrentClientError


@dataclass
class DownloadProgressSubscription:
    """一个 SSE 连接对应的筛选条件和事件队列。"""

    subscription_id: int
    client_ids: set[int]
    client_id: int | None = None
    movie_number: str | None = None
    queue: Queue = field(default_factory=Queue)
    snapshotted_client_ids: set[int] = field(default_factory=set)


@dataclass
class _ClientPoller:
    client_id: int
    stop_event: Event
    thread: Thread | None = None


class DownloadProgressHub:
    """按下载客户端共享 qB Sync 会话，并将变化扇出给 SSE 订阅者。

    当前 SSE 端点在 anyio 同步线程池中运行，每个订阅长期占用一个线程；
    单机场景足够，大规模并发前应迁 async。
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        qbittorrent_client_cls=QBittorrentClient,
    ):
        self.poll_interval_seconds = poll_interval_seconds
        self.qbittorrent_client_cls = qbittorrent_client_cls
        self._lock = RLock()
        self._next_subscription_id = 1
        self._subscriptions: dict[int, DownloadProgressSubscription] = {}
        self._pollers: dict[int, _ClientPoller] = {}
        self._torrent_cache: dict[int, dict[str, dict]] = {}
        self._managed_hashes: dict[int, set[str]] = {}
        self._client_health: dict[int, str] = {}
        self._client_transfer: dict[int, dict] = {}
        # info_hash -> {"task_id","movie_number","name"} 索引，避免每次 delta 都查 DB。
        self._task_index: dict[int, dict[str, dict]] = {}
        self._closed = False

    def subscribe(
        self,
        *,
        client_id: int | None = None,
        movie_number: str | None = None,
    ) -> DownloadProgressSubscription:
        client_ids = self._resolve_client_ids(client_id=client_id, movie_number=movie_number)
        # 订阅过滤与推送 payload 两侧都是规范番号（来自影片页 / DownloadTask 行），精确比较。
        normalized_movie_number = (movie_number or "").strip() or None
        with self._lock:
            if self._closed:
                raise RuntimeError("download progress hub is closed")
            subscription = DownloadProgressSubscription(
                subscription_id=self._next_subscription_id,
                client_ids=client_ids,
                client_id=client_id,
                movie_number=normalized_movie_number,
            )
            self._next_subscription_id += 1
            self._subscriptions[subscription.subscription_id] = subscription

        for selected_client_id in client_ids:
            self._ensure_poller(selected_client_id)
        return subscription

    def unsubscribe(self, subscription: DownloadProgressSubscription) -> None:
        with self._lock:
            self._subscriptions.pop(subscription.subscription_id, None)

    def iter_events(self, subscription: DownloadProgressSubscription, *, heartbeat_seconds: float = 15.0):
        try:
            while True:
                try:
                    yield subscription.queue.get(timeout=heartbeat_seconds)
                except Empty:
                    yield "heartbeat", {}
        finally:
            self.unsubscribe(subscription)

    def publish_task_removed(self, payload: dict) -> None:
        """控制接口删除本地任务后立即通知在线客户端，无需等待下一次 qB 轮询。"""
        client_id = payload.get("client_id")
        info_hash = payload.get("info_hash")
        if client_id is not None and info_hash is not None:
            with self._lock:
                self._task_index.get(client_id, {}).pop(info_hash, None)
        self._broadcast("download_task_removed", payload)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pollers = list(self._pollers.values())
            self._subscriptions.clear()
            self._task_index.clear()
        for poller in pollers:
            poller.stop_event.set()
        for poller in pollers:
            if poller.thread is not None:
                poller.thread.join(timeout=2.0)

    def _resolve_client_ids(self, *, client_id: int | None, movie_number: str | None) -> set[int]:
        if client_id is not None:
            if DownloadClient.get_or_none(DownloadClient.id == client_id) is None:
                raise ApiError(
                    404,
                    "download_client_not_found",
                    "Download client not found",
                    {"client_id": client_id},
                )
            return {client_id}

        query = DownloadClient.select(DownloadClient.id)
        if movie_number and movie_number.strip():
            task_client_ids = DownloadTask.select(DownloadTask.client).where(
                build_task_movie_filter(movie_number)
            )
            query = query.where(DownloadClient.id.in_(task_client_ids))
        return {item.id for item in query}

    def _ensure_poller(self, client_id: int) -> None:
        with self._lock:
            existing = self._pollers.get(client_id)
            if existing is not None and existing.thread is not None and existing.thread.is_alive():
                return
            if self._closed:
                return
            client = DownloadClient.get_or_none(DownloadClient.id == client_id)
            if client is None:
                return
            poller = _ClientPoller(client_id=client_id, stop_event=Event())
            poller.thread = Thread(
                target=self._poll_client,
                args=(poller, client),
                name=f"download-progress-{client_id}",
                daemon=True,
            )
            self._pollers[client_id] = poller
        # 预热索引放到锁外，避免长时间持锁做批量查询。
        self._prime_task_index(client_id)
        poller.thread.start()

    def _prime_task_index(self, client_id: int) -> None:
        rows = list(
            DownloadTask.select(
                DownloadTask.id,
                DownloadTask.info_hash,
                DownloadTask.movie,
                DownloadTask.name,
            ).where(DownloadTask.client == client_id)
        )
        index_snapshot = {
            row.info_hash: {
                "task_id": row.id,
                "movie_number": row.movie,
                "name": row.name,
            }
            for row in rows
        }
        with self._lock:
            self._task_index.setdefault(client_id, {}).update(index_snapshot)

    def _poll_client(self, poller: _ClientPoller, client: DownloadClient) -> None:
        # 按下载入口种类分派轮询实现；两条路径共享订阅管理与退出重启逻辑。
        try:
            if client.kind == DownloadClientKind.CLOUD115.value:
                self._poll_cloud115_loop(poller, client)
            else:
                self._poll_qbittorrent_loop(poller, client)
        finally:
            with self._lock:
                current = self._pollers.get(poller.client_id)
                if current is poller:
                    self._pollers.pop(poller.client_id, None)
                restart = not self._closed and self._has_subscribers(poller.client_id)
            if restart:
                self._ensure_poller(poller.client_id)

    def _poll_qbittorrent_loop(self, poller: _ClientPoller, client: DownloadClient) -> None:
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        while not poller.stop_event.is_set() and self._has_subscribers(poller.client_id):
            try:
                ensure_database_ready()
                delta = qb_client.get_torrent_progress_delta()
                self._mark_client_available(poller.client_id)
                self._consume_delta(poller.client_id, delta)
            except QBittorrentClientError as exc:
                self._mark_client_unavailable(poller.client_id, str(exc))
            except Exception as exc:
                logger.exception("Download progress poll failed client_id={}", poller.client_id)
                self._mark_client_unavailable(poller.client_id, str(exc))
            poller.stop_event.wait(self.poll_interval_seconds)

    def _poll_cloud115_loop(self, poller: _ClientPoller, client: DownloadClient) -> None:
        # 115 是公网 API 且有风控，轮询间隔独立配置（默认 8s），远低于 qb 的 1s。
        while not poller.stop_event.is_set() and self._has_subscribers(poller.client_id):
            try:
                ensure_database_ready()
                remote_by_hash: Dict[str, OfflineTask] = {}
                if self._has_active_cloud115_tasks(poller.client_id):
                    remote_by_hash = asyncio.run(
                        Cloud115OfflineSyncService()._fetch_remote_tasks(client)
                    )
                    self._mark_client_available(poller.client_id)
                self._consume_cloud115_tasks(poller.client_id, remote_by_hash)
            except Cloud115Error as exc:
                self._mark_client_unavailable(poller.client_id, str(exc))
            except Exception as exc:
                logger.exception("Download progress poll failed client_id={}", poller.client_id)
                self._mark_client_unavailable(poller.client_id, str(exc))
            # hot 配置：每轮等待前现读，配置刷新后无需重启 API。
            poller.stop_event.wait(settings.downloads.cloud115_progress_poll_interval_seconds)

    @staticmethod
    def _has_active_cloud115_tasks(client_id: int) -> bool:
        return DownloadTask.select().where(
            (DownloadTask.client == client_id)
            & (DownloadTask.download_state.in_(("queued", "downloading")))
        ).exists()

    def _has_subscribers(self, client_id: int) -> bool:
        with self._lock:
            return any(client_id in subscription.client_ids for subscription in self._subscriptions.values())

    def _consume_delta(self, client_id: int, delta: dict) -> None:
        # Step 1 —— 锁外把新出现 hash 的任务信息补齐到本地索引，避免锁内触 DB。
        incoming_hashes = list((delta.get("torrents") or {}).keys())
        with self._lock:
            known = set(self._task_index.get(client_id, {}).keys())
        missing_hashes = [info_hash for info_hash in incoming_hashes if info_hash not in known]
        if missing_hashes:
            fresh_rows = list(
                DownloadTask.select(
                    DownloadTask.id,
                    DownloadTask.info_hash,
                    DownloadTask.movie,
                    DownloadTask.name,
                ).where(
                    (DownloadTask.client == client_id) & (DownloadTask.info_hash.in_(missing_hashes))
                )
            )
            if fresh_rows:
                fresh_index = {
                    row.info_hash: {
                        "task_id": row.id,
                        "movie_number": row.movie,
                        "name": row.name,
                    }
                    for row in fresh_rows
                }
                with self._lock:
                    self._task_index.setdefault(client_id, {}).update(fresh_index)

        # Step 2 —— 锁内合并 cache/managed_hashes/server_state，只产出待广播条目，不做 DB / put。
        with self._lock:
            cache = self._torrent_cache.setdefault(client_id, {})
            managed_hashes = self._managed_hashes.setdefault(client_id, set())
            previous_managed_hashes = set(managed_hashes) if delta.get("full_update") else set()
            if delta.get("full_update"):
                cache.clear()
                managed_hashes.clear()

            updated_hashes: list[str] = []
            for info_hash, patch in (delta.get("torrents") or {}).items():
                merged = cache.setdefault(info_hash, {})
                merged.update(patch)
                if "tags" in patch:
                    if is_qb_managed_torrent(merged.get("tags"), client_id):
                        managed_hashes.add(info_hash)
                    else:
                        managed_hashes.discard(info_hash)
                if info_hash in managed_hashes:
                    updated_hashes.append(info_hash)

            removed_hashes: set[str] = set(delta.get("torrents_removed") or [])
            if delta.get("full_update"):
                removed_hashes.update(previous_managed_hashes - managed_hashes)
            removed_managed_hashes: list[str] = []
            for info_hash in removed_hashes:
                was_managed = info_hash in managed_hashes
                managed_hashes.discard(info_hash)
                cache.pop(info_hash, None)
                if was_managed or info_hash in previous_managed_hashes:
                    removed_managed_hashes.append(info_hash)

            server_state = delta.get("server_state") or {}
            if server_state and server_state != self._client_transfer.get(client_id):
                self._client_transfer[client_id] = dict(server_state)
                server_state_snapshot = dict(server_state)
            else:
                server_state_snapshot = None

            # 决定哪些订阅这一轮需要收到 snapshot；同时快照当前受管态供后续构建。
            pending_snapshot_subscriptions = [
                subscription
                for subscription in self._subscriptions.values()
                if client_id in subscription.client_ids
                and client_id not in subscription.snapshotted_client_ids
            ]
            for subscription in pending_snapshot_subscriptions:
                subscription.snapshotted_client_ids.add(client_id)
            snapshot_hashes = list(managed_hashes)
            snapshot_cache = {info_hash: dict(cache.get(info_hash, {})) for info_hash in snapshot_hashes}
            update_cache = {info_hash: dict(cache.get(info_hash, {})) for info_hash in updated_hashes}
            task_index_snapshot = dict(self._task_index.get(client_id, {}))
            broadcast_subscriptions = list(self._subscriptions.values())

        # Step 3 —— 锁外构造 payload、扇出给订阅者。
        snapshot_items: list[dict] = []
        for info_hash in snapshot_hashes:
            resource = self._build_progress_resource(
                client_id,
                info_hash,
                snapshot_cache.get(info_hash, {}),
                task_index_snapshot,
            )
            if resource is not None:
                snapshot_items.append(resource.model_dump(mode="json"))

        for subscription in pending_snapshot_subscriptions:
            filtered_items = [
                item for item in snapshot_items if self._matches_subscription(subscription, item)
            ]
            subscription.queue.put(("snapshot", {"client_id": client_id, "items": filtered_items}))

        # 本轮刚拿到 snapshot 的订阅当轮不再收 update：snapshot 已含最新态，避免同数据渲染两次。
        pending_snapshot_ids = {sub.subscription_id for sub in pending_snapshot_subscriptions}
        update_subscriptions = [
            sub for sub in broadcast_subscriptions if sub.subscription_id not in pending_snapshot_ids
        ]
        for info_hash in updated_hashes:
            resource = self._build_progress_resource(
                client_id,
                info_hash,
                update_cache.get(info_hash, {}),
                task_index_snapshot,
            )
            if resource is None:
                continue
            payload = resource.model_dump(mode="json")
            self._dispatch(update_subscriptions, "download_task_updated", payload)

        for info_hash in removed_managed_hashes:
            payload = self._build_removed_payload(client_id, info_hash, task_index_snapshot)
            with self._lock:
                self._task_index.get(client_id, {}).pop(info_hash, None)
            if payload is None:
                continue
            self._dispatch(broadcast_subscriptions, "download_task_removed", payload)

        if server_state_snapshot is not None:
            transfer_payload = DownloadClientTransferResource(
                client_id=client_id,
                download_speed_bytes=self._as_int(server_state_snapshot.get("dl_info_speed")),
                upload_speed_bytes=self._as_int(server_state_snapshot.get("up_info_speed")),
                connection_status=self._as_optional_str(server_state_snapshot.get("connection_status")),
            ).model_dump(mode="json")
            self._dispatch(broadcast_subscriptions, "download_client_status", transfer_payload)

    def _consume_cloud115_tasks(
        self, client_id: int, remote_by_hash: Dict[str, OfflineTask]
    ) -> None:
        """把 115 离线任务全量快照 diff 后扇出给订阅者。

        与 qb 的 delta 流不同：cloud115 每轮直接查 DB 拿本地任务清单（8s 间隔下代价可忽略，
        且天然吸收新提交/已删除的任务），远端数据只用于补活跃任务进度；终态完全使用数据库快照。
        """
        rows = list(DownloadTask.select().where(DownloadTask.client == client_id))
        items: list[dict] = []
        for row in rows:
            remote = remote_by_hash.get(row.info_hash)
            items.append(self._build_cloud115_resource(client_id, row, remote).model_dump(mode="json"))

        with self._lock:
            cache = self._torrent_cache.setdefault(client_id, {})
            pending_snapshot_subscriptions = [
                subscription
                for subscription in self._subscriptions.values()
                if client_id in subscription.client_ids
                and client_id not in subscription.snapshotted_client_ids
            ]
            for subscription in pending_snapshot_subscriptions:
                subscription.snapshotted_client_ids.add(client_id)
            broadcast_subscriptions = list(self._subscriptions.values())
            # 与上一轮 payload 逐条比对，只广播有变化的条目。
            updated_items = [
                item for item in items if cache.get(item["info_hash"]) != item
            ]
            cache.clear()
            cache.update({item["info_hash"]: item for item in items})

        for subscription in pending_snapshot_subscriptions:
            filtered_items = [
                item for item in items if self._matches_subscription(subscription, item)
            ]
            subscription.queue.put(("snapshot", {"client_id": client_id, "items": filtered_items}))

        pending_snapshot_ids = {sub.subscription_id for sub in pending_snapshot_subscriptions}
        update_subscriptions = [
            sub for sub in broadcast_subscriptions if sub.subscription_id not in pending_snapshot_ids
        ]
        for item in updated_items:
            self._dispatch(update_subscriptions, "download_task_updated", item)

    @staticmethod
    def _build_cloud115_resource(
        client_id: int, task: DownloadTask, remote: OfflineTask | None
    ) -> DownloadTaskProgressResource:
        # 远端没拉到该任务时退回本地库内状态（分页上限内可能没拉全，不清零进度）。
        if remote is None:
            return DownloadTaskProgressResource(
                task_id=task.id,
                client_id=client_id,
                movie_number=task.movie,
                name=task.name,
                info_hash=task.info_hash,
                progress=task.progress,
                raw_state=task.download_state,
                download_state=task.download_state,
                download_speed_bytes=0,
                uploaded_speed_bytes=0,
                downloaded_bytes=0,
                total_size_bytes=0,
                eta_seconds=None,
            )
        return DownloadTaskProgressResource(
            task_id=task.id,
            client_id=client_id,
            movie_number=task.movie,
            name=remote.name or task.name,
            info_hash=task.info_hash,
            progress=round(remote.percent_done / 100.0, 4),
            raw_state=str(remote.status),
            download_state=CLOUD115_OFFLINE_STATE_MAP.get(remote.status) or task.download_state,
            download_speed_bytes=remote.rate_download,
            uploaded_speed_bytes=0,
            downloaded_bytes=int(remote.size * remote.percent_done / 100.0),
            total_size_bytes=remote.size,
            eta_seconds=remote.left_time_seconds or None,
        )

    def _dispatch(self, subscriptions: list[DownloadProgressSubscription], event: str, payload: dict) -> None:
        for subscription in subscriptions:
            if event == "download_client_status":
                # 客户端状态属于订阅下载客户端本身，按番号过滤时也必须可见，否则客户端无法解释进度停滞。
                matches = payload.get("client_id") in subscription.client_ids
            else:
                matches = self._matches_subscription(subscription, payload)
            if matches:
                subscription.queue.put((event, payload))

    def _mark_client_available(self, client_id: int) -> None:
        with self._lock:
            if self._client_health.get(client_id) == "available":
                return
            self._client_health[client_id] = "available"
        self._broadcast("download_client_status", {"client_id": client_id, "status": "available"})

    def _mark_client_unavailable(self, client_id: int, detail: str) -> None:
        with self._lock:
            if self._client_health.get(client_id) == "unavailable":
                return
            self._client_health[client_id] = "unavailable"
        self._broadcast(
            "download_client_status",
            {"client_id": client_id, "status": "unavailable", "message": detail},
        )

    def _broadcast(self, event: str, payload: dict) -> None:
        with self._lock:
            subscriptions = list(self._subscriptions.values())
        self._dispatch(subscriptions, event, payload)

    def _build_progress_resource(
        self,
        client_id: int,
        info_hash: str,
        torrent: dict,
        task_index: dict[str, dict],
    ) -> Optional[DownloadTaskProgressResource]:
        task_info = task_index.get(info_hash)
        if task_info is None:
            # 未在本地任务表登记的种子即使打了标签，也不向客户端泄露。
            return None
        raw_state = str(torrent.get("state") or "")
        eta_seconds = self._as_optional_int(torrent.get("eta"))
        if eta_seconds is not None and eta_seconds >= QB_ETA_INFINITY:
            eta_seconds = None
        return DownloadTaskProgressResource(
            task_id=task_info["task_id"],
            client_id=client_id,
            movie_number=task_info.get("movie_number"),
            name=str(torrent.get("name") or task_info.get("name") or ""),
            info_hash=info_hash,
            progress=self._as_float(torrent.get("progress")),
            raw_state=raw_state,
            # 与对账用同一判定，避免实时流和数据库对同一个种子给出不同状态。
            # sync 增量包里没有 last_activity 时函数自动退回 map_download_state 的结果。
            download_state=resolve_qbittorrent_download_state(
                raw_state, torrent.get("last_activity")
            ),
            download_speed_bytes=self._as_int(torrent.get("dlspeed")),
            uploaded_speed_bytes=self._as_int(torrent.get("upspeed")),
            downloaded_bytes=self._as_int(torrent.get("downloaded")),
            total_size_bytes=self._as_int(torrent.get("size") or torrent.get("total_size")),
            eta_seconds=eta_seconds,
        )

    @staticmethod
    def _build_removed_payload(
        client_id: int,
        info_hash: str,
        task_index: dict[str, dict],
    ) -> Optional[dict]:
        task_info = task_index.get(info_hash)
        if task_info is None:
            return None
        return {
            "task_id": task_info["task_id"],
            "client_id": client_id,
            "movie_number": task_info.get("movie_number"),
            "info_hash": info_hash,
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

    @staticmethod
    def _as_optional_str(value: object) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _matches_subscription(subscription: DownloadProgressSubscription, payload: dict) -> bool:
        payload_client_id = payload.get("client_id")
        if payload_client_id is not None and payload_client_id not in subscription.client_ids:
            return False
        if subscription.movie_number is None:
            return True
        return str(payload.get("movie_number") or "").strip() == subscription.movie_number
