from src.model import (
    DownloadClient,
    DownloadSubmissionRecord,
    DownloadTask,
    MediaLibrary,
)
from src.plugins.provider_protocol import RemoteDownloadTask
from src.schema.transfers.downloads import DownloadRequestCreateRequest
from src.service.transfers.downloads import request_service as request_service_module
from src.service.transfers.downloads.request_service import DownloadRequestService

HASH = "0123456789abcdef0123456789abcdef01234567"


class FakeProvider:
    def submit(self, *, submission):
        return RemoteDownloadTask(
            remote_id=HASH,
            name=submission.display_name,
            state="queued",
            progress=0,
            completed_source_ref=None,
        )


def _context(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="snapshot-library",
        provider_key="demo",
        provider_config={},
    )
    client = DownloadClient.create(
        name="snapshot-client",
        library=library,
        provider_config={},
    )
    monkeypatch.setattr(
        request_service_module,
        "download_provider",
        lambda _client: FakeProvider(),
    )
    return client


def test_indexer_submission_persists_clean_release_fields(test_db, monkeypatch):
    client = _context(test_db, monkeypatch)
    service = DownloadRequestService()
    monkeypatch.setattr(service, "_resolve_client", lambda _payload: client)

    service.create_request(
        DownloadRequestCreateRequest(
            movie_number="ABC-001",
            candidate={
                "source_uri": f"magnet:?xt=urn:btih:{HASH}",
                "indexer_name": "indexer-a",
                "title": "ABC-001 1080p 中文字幕",
                "release_title": "ABC-001 1080p",
                "description": "中文字幕 source description",
                "size_bytes": 1024,
                "seeders": 10,
            },
        )
    )

    record = DownloadSubmissionRecord.get()
    assert record.title == "ABC-001 1080p 中文字幕"
    assert record.release_title == "ABC-001 1080p"
    assert record.description == "中文字幕 source description"
    assert DownloadTask.get_by_id(record.task_id).movie == "ABC-001"


def test_manual_magnet_uses_dn_as_clean_release_title(test_db, monkeypatch):
    client = _context(test_db, monkeypatch)
    service = DownloadRequestService()

    service.create_manual_request(
        client_id=client.id,
        media_kind="jav",
        source_uri=f"magnet:?dn=Clean%20Release&xt=urn:btih:{HASH}",
        movie_number="ABC-002",
    )

    record = DownloadSubmissionRecord.get()
    assert record.release_title == "Clean Release"
    assert record.description is None
