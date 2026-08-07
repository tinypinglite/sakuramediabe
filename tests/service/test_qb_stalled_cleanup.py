from datetime import timedelta

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.model.playback.libraries import MediaLibrary
from src.service.transfers.downloads.stalled_cleanup_service import (
    QBStalledCleanupService,
)
from src.service.transfers.downloads.sync_service import DownloadSyncService


class FakeQBClient:
    """内存版 qB 客户端：list_torrents 返回注入的种子，delete_torrent 记录调用。

    种子列表与删除记录由工厂持有（from_download_client 返回的新实例共享同一份状态），
    因为服务侧以类方式注入（qbittorrent_client_cls=...）再逐客户端构造实例。
    """

    torrents: list[dict] = []
    deleted: list[tuple[str, bool]] = []

    def __init__(self, torrents=None):
        self.torrents = torrents if torrents is not None else type(self).torrents
        self.deleted = type(self).deleted

    @classmethod
    def from_download_client(cls, _download_client):
        return cls()

    def list_torrents(self, *, client_id: int | None = None):
        return self.torrents

    def delete_torrent(self, info_hash: str, *, client_id: int, delete_files: bool) -> bool:
        self.deleted.append((info_hash, delete_files))
        return True


@pytest.fixture()
def qb_client_cls(monkeypatch):
    """重置类级共享状态的 FakeQBClient 工厂。"""

    class _FakeQBClient(FakeQBClient):
        pass

    _FakeQBClient.torrents = []
    _FakeQBClient.deleted = []
    return _FakeQBClient


@pytest.fixture()
def qb_env(test_db):
    library = MediaLibrary.create(
        name="local-downloads", backend="local", backend_config={}
    )
    client = DownloadClient.create(
        name="qb-main",
        kind="qbittorrent",
        base_url="http://qb:8080",
        username="admin",
        password="secret",
        client_save_path="/downloads",
        local_root_path="/mnt/downloads",
        media_library=library,
    )
    return library, client


def _remote_torrent(
    info_hash: str, state: str, progress: float = 0.5, last_activity: int | None = 1785522822
) -> dict:
    return {
        "info_hash": info_hash,
        "name": "ABP-001",
        "state": state,
        "progress": progress,
        "save_path": "/downloads/ABP-001",
        "last_activity": last_activity,
    }


def test_sync_sets_started_at_on_active_states_and_clears_on_leave(test_db, qb_env, qb_client_cls):
    """对账维护 download_started_at：进入活跃下载态起算，离开即清空。"""
    _, client = qb_env
    task = DownloadTask.create(
        client=client,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        download_state="downloading",
    )
    fake_cls = qb_client_cls

    # 仍在 downloading：started_at 从空写入。
    fake_cls.torrents = [_remote_torrent("a" * 40, "downloading")]
    DownloadSyncService(qbittorrent_client_cls=fake_cls).sync_client(client.id)
    task = DownloadTask.get_by_id(task.id)
    assert task.download_started_at is not None

    # 变排队（queuedDL）：排队时长不计，清空开始时刻。
    fake_cls.torrents = [_remote_torrent("a" * 40, "queuedDL")]
    DownloadSyncService(qbittorrent_client_cls=fake_cls).sync_client(client.id)
    task = DownloadTask.get_by_id(task.id)
    assert task.download_started_at is None

    # 重新回到 stalledDL：重新起算。
    fake_cls.torrents = [_remote_torrent("a" * 40, "stalledDL")]
    DownloadSyncService(qbittorrent_client_cls=fake_cls).sync_client(client.id)
    task = DownloadTask.get_by_id(task.id)
    assert task.download_started_at is not None


def test_cleanup_deletes_stalled_torrent_and_marks_dead(test_db, qb_env, qb_client_cls):
    """命中清理：删种 + 删文件 + 本地行落 stalled_dead。"""
    _, client = qb_env
    started_at = utc_now_for_db() - timedelta(hours=25)
    task = DownloadTask.create(
        client=client,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        download_state="downloading",
        download_started_at=started_at,
    )
    qb_client_cls.torrents = [_remote_torrent("a" * 40, "stalledDL", progress=0.2)]

    stats = QBStalledCleanupService(qbittorrent_client_cls=qb_client_cls).cleanup_stalled_tasks()

    assert stats["cleaned_count"] == 1
    # 删除必须连带删文件。
    assert qb_client_cls.deleted == [("a" * 40, True)]
    task = DownloadTask.get_by_id(task.id)
    assert task.download_state == "stalled_dead"
    # 落死态同时清空 started_at：恢复"离开活跃态即清空"不变量，手动重加同一 hash
    # 后对账会重新起算 24h 宽限，而不是拿旧时间戳次日秒删。
    assert task.download_started_at is None


def test_cleanup_falls_back_to_last_activity_when_started_at_missing(test_db, qb_env, qb_client_cls):
    """started_at 为空（存量 7 天判死清空了计时）时回退 qB last_activity 兜底判定。"""
    _, client = qb_env
    old_ts = int(utc_now_for_db().timestamp()) - 25 * 3600
    task = DownloadTask.create(
        client=client,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        download_state="stalled_dead",
        download_started_at=None,
    )
    qb_client_cls.torrents = [
        _remote_torrent("a" * 40, "stalledDL", progress=0.2, last_activity=old_ts)
    ]

    stats = QBStalledCleanupService(qbittorrent_client_cls=qb_client_cls).cleanup_stalled_tasks()

    assert stats["cleaned_count"] == 1
    assert qb_client_cls.deleted == [("a" * 40, True)]
    task = DownloadTask.get_by_id(task.id)
    assert task.download_state == "stalled_dead"
    assert task.download_started_at is None


def test_cleanup_skips_missing_started_at_with_recent_activity(test_db, qb_env, qb_client_cls):
    """started_at 为空且 last_activity 未超时（刚被对账清空但种子仍有动静）：不清理。"""
    _, client = qb_env
    recent_ts = int(utc_now_for_db().timestamp()) - 2 * 3600
    DownloadTask.create(
        client=client,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        download_state="stalled",
        download_started_at=None,
    )
    qb_client_cls.torrents = [
        _remote_torrent("a" * 40, "stalledDL", progress=0.2, last_activity=recent_ts)
    ]

    stats = QBStalledCleanupService(qbittorrent_client_cls=qb_client_cls).cleanup_stalled_tasks()

    assert stats["cleaned_count"] == 0
    assert qb_client_cls.deleted == []


def test_cleanup_skips_queued_paused_young_and_unstarted(test_db, qb_env, qb_client_cls):
    """永不清理：queuedDL（排队）/ pausedDL（用户暂停）/ 未超时 / started_at 为空。"""
    _, client = qb_env
    now = utc_now_for_db()
    old = now - timedelta(hours=25)
    queued = DownloadTask.create(
        client=client, name="queued", info_hash="a" * 40,
        save_path="/s", download_state="queued", download_started_at=old,
    )
    paused = DownloadTask.create(
        client=client, name="paused", info_hash="b" * 40,
        save_path="/s", download_state="paused", download_started_at=old,
    )
    young = DownloadTask.create(
        client=client, name="young", info_hash="c" * 40,
        save_path="/s", download_state="downloading", download_started_at=now,
    )
    unstarted = DownloadTask.create(
        client=client, name="unstarted", info_hash="d" * 40,
        save_path="/s", download_state="downloading", download_started_at=None,
    )
    qb_client_cls.torrents = [
        _remote_torrent("a" * 40, "queuedDL"),
        _remote_torrent("b" * 40, "stoppedDL"),
        _remote_torrent("c" * 40, "downloading"),
        # started_at 为空 + last_activity 最近：兜底也不命中，保持跳过（存量行安全）。
        _remote_torrent(
            "d" * 40,
            "downloading",
            last_activity=int(utc_now_for_db().timestamp()) - 60,
        ),
    ]

    stats = QBStalledCleanupService(qbittorrent_client_cls=qb_client_cls).cleanup_stalled_tasks()

    assert stats["cleaned_count"] == 0
    assert qb_client_cls.deleted == []
    for task_id in (queued.id, paused.id, young.id, unstarted.id):
        task = DownloadTask.get_by_id(task_id)
        assert task.download_state in ("queued", "paused", "downloading")


def test_cleanup_skips_torrents_without_local_row(test_db, qb_env, qb_client_cls):
    """远程种子无本地行（非系统提交）：不清理。"""
    _, client = qb_env
    qb_client_cls.torrents = [_remote_torrent("e" * 40, "stalledDL")]

    stats = QBStalledCleanupService(qbittorrent_client_cls=qb_client_cls).cleanup_stalled_tasks()

    assert stats["cleaned_count"] == 0
    assert qb_client_cls.deleted == []


def test_prune_ghost_tasks_keeps_dead_rows(test_db, qb_env):
    """反向对账豁免死态行：删种后黑名单台账必须保留，否则下一轮自动下载会把同一死种拉回来。"""
    _, client = qb_env
    dead = DownloadTask.create(
        client=client, name="dead", info_hash="a" * 40,
        save_path="/s", download_state="stalled_dead",
    )
    live = DownloadTask.create(
        client=client, name="live", info_hash="b" * 40,
        save_path="/s", download_state="downloading",
    )

    # remote 只剩一个不相关的 hash：dead 与 live 都不在 remote，但 dead 豁免保留。
    removed = DownloadSyncService()._prune_ghost_tasks(client.id, {"f" * 40})

    assert removed == 1
    assert DownloadTask.get_by_id(dead.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == live.id) is None
