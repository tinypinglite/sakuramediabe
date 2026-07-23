from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.service.transfers import download_task_service as download_task_service_module
from src.service.transfers.common import is_download_complete, map_download_state
from src.service.transfers.download_task_service import DownloadTaskService


@pytest.mark.parametrize("raw_state", ["stoppedUP", "pausedUP"])
def test_stopped_complete_torrents_remain_eligible_for_import(raw_state):
    normalized = map_download_state(raw_state)

    assert normalized == "completed"
    assert is_download_complete(normalized) is True


@pytest.mark.parametrize("raw_state", ["stoppedDL", "pausedDL"])
def test_stopped_incomplete_torrents_remain_paused(raw_state):
    normalized = map_download_state(raw_state)

    assert normalized == "paused"
    assert is_download_complete(normalized) is False


@pytest.mark.parametrize(
    ("initial_state", "expected_state"),
    [
        ("seeding", "completed"),
        ("completed", "completed"),
        ("downloading", "paused"),
        ("stalled", "paused"),
    ],
)
def test_pause_persists_the_normalized_state_immediately(
    monkeypatch,
    initial_state,
    expected_state,
):
    task = SimpleNamespace(
        id=9,
        client_id=2,
        client=SimpleNamespace(kind="qbittorrent"),
        info_hash="test-hash",
        download_state=initial_state,
        updated_at=None,
        save=Mock(),
    )
    monkeypatch.setattr(download_task_service_module, "require_task", lambda _task_id: task)

    class FakeQBittorrentClient:
        paused_hashes = []

        @classmethod
        def from_download_client(cls, _client):
            return cls()

        def pause_torrent(self, info_hash, *, client_id):
            self.paused_hashes.append((info_hash, client_id))

    response = DownloadTaskService.pause_task(
        task.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
    )

    assert response.task_id == task.id
    assert task.download_state == expected_state
    assert task.updated_at is not None
    task.save.assert_called_once_with()
    assert FakeQBittorrentClient.paused_hashes == [(task.info_hash, task.client_id)]
