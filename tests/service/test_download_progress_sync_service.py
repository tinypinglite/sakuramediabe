from datetime import timedelta
from unittest.mock import Mock

import peewee
import pytest
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.model.playback.libraries import MediaLibrary
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.downloads.progress_sync_service import (
    DownloadProgressSyncService,
)


class FakeQBClient:
    torrents: list[dict] = []
    error: Exception | None = None
    close_calls = 0

    @classmethod
    def from_download_client(cls, _client):
        return cls()

    def list_torrents(self, *, client_id: int):
        if self.error is not None:
            raise self.error
        return self.torrents

    def close(self):
        type(self).close_calls += 1


class PerClientQBClient:
    torrents_by_client_id: dict[int, list[dict]] = {}

    def __init__(self, client_id: int):
        self.client_id = client_id

    @classmethod
    def from_download_client(cls, client):
        return cls(client.id)

    def list_torrents(self, *, client_id: int):
        assert client_id == self.client_id
        return self.torrents_by_client_id.get(client_id, [])

    def close(self):
        return None


@pytest.fixture()
def qb_env(test_db):
    library = MediaLibrary.create(name="downloads", backend="local", backend_config={})
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
    return client


def _remote_task(info_hash: str, **overrides) -> dict:
    task = {
        "info_hash": info_hash,
        "progress": 0.5,
        "state": "downloading",
        "last_activity": int(utc_now_for_db().timestamp()) - 60,
        "dlspeed": 2_048,
        "upspeed": 128,
        "downloaded": 1_024,
        "size": 2_048,
        "eta": 60,
    }
    task.update(overrides)
    return task


def test_qb_client_close_releases_both_http_connection_pools():
    qb_api_client = Mock()
    torrent_http_client = Mock()
    client = QBittorrentClient(
        base_url="http://qb:8080",
        username="admin",
        password="secret",
        client=qb_api_client,
        http_client=torrent_http_client,
    )

    client.close()

    qb_api_client._trigger_session_initialization.assert_called_once_with()
    torrent_http_client.close.assert_called_once_with()


def test_sync_updates_existing_task_snapshot_without_creating_remote_only_task(qb_env):
    existing = DownloadTask.create(
        client=qb_env,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
    )
    FakeQBClient.torrents = [
        _remote_task("a" * 40),
        _remote_task("b" * 40, state="queuedDL"),
    ]
    FakeQBClient.error = None

    summary = DownloadProgressSyncService(qbittorrent_client_cls=FakeQBClient).sync_client(qb_env.id)

    task = DownloadTask.get_by_id(existing.id)
    assert summary == {
        "client_id": qb_env.id,
        "scanned_count": 2,
        "updated_count": 1,
        "unchanged_count": 0,
    }
    assert DownloadTask.select().count() == 1
    assert task.progress == 0.5
    assert task.download_state == "downloading"
    assert task.raw_state == "downloading"
    assert task.download_speed_bytes == 2_048
    assert task.uploaded_speed_bytes == 128
    assert task.downloaded_bytes == 1_024
    assert task.total_size_bytes == 2_048
    assert task.eta_seconds == 60
    assert task.progress_synced_at is not None


def test_sync_skips_write_when_snapshot_values_are_unchanged(qb_env):
    synced_at = utc_now_for_db() - timedelta(minutes=5)
    task = DownloadTask.create(
        client=qb_env,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        progress=0.5,
        download_state="downloading",
        raw_state="downloading",
        download_speed_bytes=2_048,
        uploaded_speed_bytes=128,
        downloaded_bytes=1_024,
        total_size_bytes=2_048,
        eta_seconds=60,
        progress_synced_at=synced_at,
    )
    updated_at = task.updated_at
    FakeQBClient.torrents = [_remote_task("a" * 40)]
    FakeQBClient.error = None

    summary = DownloadProgressSyncService(qbittorrent_client_cls=FakeQBClient).sync_client(qb_env.id)

    task = DownloadTask.get_by_id(task.id)
    assert summary["updated_count"] == 0
    assert summary["unchanged_count"] == 1
    assert task.progress_synced_at == synced_at
    assert task.updated_at == updated_at


def test_sync_preserves_previous_snapshot_when_qb_request_fails(qb_env):
    task = DownloadTask.create(
        client=qb_env,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
        raw_state="stalledDL",
        download_speed_bytes=512,
        progress_synced_at=utc_now_for_db(),
    )
    previous_synced_at = task.progress_synced_at
    FakeQBClient.error = QBittorrentClientError("connection refused")
    FakeQBClient.close_calls = 0

    with pytest.raises(ApiError, match="qBittorrent request failed") as exc:
        DownloadProgressSyncService(qbittorrent_client_cls=FakeQBClient).sync_client(qb_env.id)

    task = DownloadTask.get_by_id(task.id)
    assert exc.value.code == "download_progress_sync_failed"
    assert task.raw_state == "stalledDL"
    assert task.download_speed_bytes == 512
    assert task.progress_synced_at == previous_synced_at
    assert FakeQBClient.close_calls == 1


def test_sync_all_clients_skips_qb_request_when_client_has_no_local_tasks(qb_env):
    class ForbiddenQBClient:
        @classmethod
        def from_download_client(cls, _client):
            raise AssertionError("qB progress sync must not request an empty client")

    summary = DownloadProgressSyncService(
        qbittorrent_client_cls=ForbiddenQBClient
    ).sync_all_clients()

    assert summary == {
        "total_clients": 1,
        "scanned_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "failed_count": 0,
        "failed_client_ids": [],
    }


def test_cloud115_only_clients_never_construct_or_request_qb_client(test_db):
    library = MediaLibrary.create(name="cloud-downloads", backend="local", backend_config={})
    cloud_client = DownloadClient.create(
        name="cloud115-main",
        kind="cloud115",
        media_library=library,
    )

    class ForbiddenQBClient:
        @classmethod
        def from_download_client(cls, _client):
            raise AssertionError("Cloud115 progress sync must not construct a qB client")

    service = DownloadProgressSyncService(qbittorrent_client_cls=ForbiddenQBClient)

    assert service.sync_client(cloud_client.id) == {
        "client_id": cloud_client.id,
        "scanned_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
    }
    assert service.sync_all_clients() == {
        "total_clients": 0,
        "scanned_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "failed_count": 0,
        "failed_client_ids": [],
    }


def test_sync_all_clients_keeps_same_hash_isolated_by_client(qb_env):
    second_client = DownloadClient.create(
        name="qb-secondary",
        kind="qbittorrent",
        base_url="http://qb-secondary:8080",
        username="admin",
        password="secret",
        client_save_path="/downloads",
        local_root_path="/mnt/downloads",
        media_library=qb_env.media_library,
    )
    first_task = DownloadTask.create(
        client=qb_env,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
    )
    second_task = DownloadTask.create(
        client=second_client,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
    )
    PerClientQBClient.torrents_by_client_id = {
        qb_env.id: [_remote_task("a" * 40, dlspeed=1_000)],
        second_client.id: [_remote_task("a" * 40, dlspeed=2_000)],
    }

    summary = DownloadProgressSyncService(
        qbittorrent_client_cls=PerClientQBClient
    ).sync_all_clients()

    assert summary["updated_count"] == 2
    assert DownloadTask.get_by_id(first_task.id).download_speed_bytes == 1_000
    assert DownloadTask.get_by_id(second_task.id).download_speed_bytes == 2_000


def test_sync_all_clients_isolates_expected_api_failure(qb_env, monkeypatch):
    second_client = DownloadClient.create(
        name="qb-secondary",
        kind="qbittorrent",
        base_url="http://qb-secondary:8080",
        username="admin",
        password="secret",
        client_save_path="/downloads",
        local_root_path="/mnt/downloads",
        media_library=qb_env.media_library,
    )
    service = DownloadProgressSyncService(qbittorrent_client_cls=FakeQBClient)

    def sync_one_client(client_id):
        if client_id == qb_env.id:
            raise ApiError(502, "download_progress_sync_failed", "qBittorrent request failed")
        return {
            "client_id": second_client.id,
            "scanned_count": 1,
            "updated_count": 1,
            "unchanged_count": 0,
        }

    monkeypatch.setattr(service, "sync_client", sync_one_client)

    summary = service.sync_all_clients()

    assert summary["total_clients"] == 2
    assert summary["updated_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["failed_client_ids"] == [qb_env.id]


def test_sync_all_clients_logs_qb_failure_once(qb_env):
    messages = []
    DownloadTask.create(
        client=qb_env,
        name="ABP-001",
        info_hash="a" * 40,
        save_path="/mnt/downloads/ABP-001",
    )
    FakeQBClient.error = QBittorrentClientError("connection refused")
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        summary = DownloadProgressSyncService(
            qbittorrent_client_cls=FakeQBClient
        ).sync_all_clients()
    finally:
        logger.remove(sink_id)

    matching = [message for message in messages if "client_id=" in message]
    assert summary["failed_client_ids"] == [qb_env.id]
    assert len(matching) == 1


def test_sync_all_clients_propagates_database_failure(qb_env, monkeypatch):
    service = DownloadProgressSyncService(qbittorrent_client_cls=FakeQBClient)

    def fail_with_database_error(_client_id):
        raise peewee.OperationalError("database unavailable")

    monkeypatch.setattr(service, "sync_client", fail_with_database_error)

    with pytest.raises(peewee.OperationalError, match="database unavailable"):
        service.sync_all_clients()
