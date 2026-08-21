from unittest.mock import Mock

from src.model import DownloadClient
from src.model.playback.libraries import MediaLibrary
from src.service.transfers.cloud115.offline.sync_service import (
    Cloud115OfflineSyncService,
)


def test_sync_client_skips_115_request_without_active_tasks(test_db):
    library = MediaLibrary.create(
        name="cloud115-empty-sync",
        backend="local",
        backend_config={},
    )
    client = DownloadClient.create(
        name="cloud115-empty-sync",
        kind="cloud115",
        media_library=library,
    )
    service = Cloud115OfflineSyncService()
    service._fetch_remote_tasks = Mock(
        side_effect=AssertionError("empty cloud115 client must not request 115")
    )

    assert service.sync_client(client) == {
        "updated_count": 0,
        "import_triggered_count": 0,
        "abandoned_count": 0,
    }
    service._fetch_remote_tasks.assert_not_called()
