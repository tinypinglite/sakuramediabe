from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    DownloadClient,
    DownloadSubmissionRecord,
    DownloadTask,
    MediaLibrary,
    VideoCollection,
)
from src.plugins.provider_protocol import RemoteDownloadTask
from src.service.transfers.downloads import download_source as download_source_module
from src.service.transfers.downloads import request_service as request_service_module
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.imports.import_service import MediaImportService
from src.service.transfers.shared.import_task_service import ImportTaskService

HASH = "0123456789abcdef0123456789abcdef01234567"


class FakeProvider:
    def __init__(self) -> None:
        self.submissions = []

    def submit(self, *, submission):
        self.submissions.append(submission)
        return RemoteDownloadTask(
            remote_id=HASH,
            name=submission.display_name,
            state="queued",
            progress=0.0,
            completed_source_ref=None,
        )


@pytest.fixture
def manual_context(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="manual-library", provider_key="demo", provider_config={}
    )
    client = DownloadClient.create(
        name="manual-client", library=library, provider_config={}
    )
    provider = FakeProvider()
    monkeypatch.setattr(
        request_service_module, "download_provider", lambda _client: provider
    )
    monkeypatch.setattr(
        request_service_module.MEDIA_PROVIDER_REGISTRY,
        "supports_torrent_bytes",
        lambda _provider_key: True,
    )
    monkeypatch.setattr(
        request_service_module.MEDIA_PROVIDER_REGISTRY,
        "supported_source_kinds",
        lambda _provider_key: {"magnet", "torrent", "ed2k", "http"},
    )
    return SimpleNamespace(library=library, client=client, provider=provider)


def test_manual_jav_magnet_creates_task_without_indexer(manual_context):
    result = DownloadRequestService().create_manual_request(
        client_id=manual_context.client.id,
        media_kind="jav",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
        movie_number="abc-123",
    )

    task = DownloadTask.get_by_id(result.task.id)
    assert result.created is True
    assert task.media_kind == "jav"
    assert task.movie == "ABC-123"
    assert task.collection_id is None
    assert task.display_name is None
    assert task.name == f"ABC-123-{HASH[:6]}"
    record = DownloadSubmissionRecord.get()
    assert record.indexer_name == "manual"
    assert record.movie_number == "ABC-123"
    assert record.title == "ABC-123"
    assert record.source_uri == f"magnet:?xt=urn:btih:{HASH}"
    assert record.source_kind == "magnet"
    assert record.resource_key == f"bt:{HASH}"
    assert manual_context.provider.submissions[0].display_name == f"ABC-123-{HASH[:6]}"
    assert manual_context.provider.submissions[0].torrent_bytes is None


def test_manual_video_torrent_bytes_prefers_provider_bytes(
    manual_context, monkeypatch
):
    collection = VideoCollection.create(name="PornBox 合集")
    monkeypatch.setattr(
        download_source_module,
        "resolve_torrent_upload",
        lambda _payload: (HASH, "Uploaded Torrent"),
    )

    result = DownloadRequestService().create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        torrent_bytes=b"torrent-payload",
        collection_id=collection.id,
        display_name="用户命名",
        delete_source_after_import=True,
    )

    task = DownloadTask.get_by_id(result.task.id)
    submission = manual_context.provider.submissions[0]
    assert task.media_kind == "video"
    assert task.movie is None
    assert task.collection_id == collection.id
    assert task.display_name == "用户命名"
    assert task.delete_source_after_import is True
    assert submission.source_uri == f"magnet:?xt=urn:btih:{HASH}"
    assert submission.source_kind == "torrent"
    assert submission.resource_key == f"bt:{HASH}"
    assert submission.torrent_bytes == b"torrent-payload"
    assert submission.display_name == f"用户命名-{HASH[:6]}"
    assert DownloadSubmissionRecord.get().movie_number == ""


def test_manual_torrent_degrades_to_magnet_without_capability(
    manual_context, monkeypatch
):
    monkeypatch.setattr(
        download_source_module,
        "resolve_torrent_upload",
        lambda _payload: (HASH, "Uploaded Torrent"),
    )
    monkeypatch.setattr(
        request_service_module.MEDIA_PROVIDER_REGISTRY,
        "supports_torrent_bytes",
        lambda _provider_key: False,
    )

    DownloadRequestService().create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        torrent_bytes=b"torrent-payload",
    )

    submission = manual_context.provider.submissions[0]
    assert submission.torrent_bytes is None
    assert submission.source_uri == f"magnet:?xt=urn:btih:{HASH}"
    assert submission.display_name == f"Uploaded Torrent-{HASH[:6]}"


def test_manual_jav_requires_movie_number(manual_context):
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="jav",
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
        )

    assert error.value.code == "invalid_download_request_movie_number"
    assert manual_context.provider.submissions == []


def test_manual_video_rejects_missing_collection(manual_context):
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="video",
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            collection_id=9999,
        )

    assert error.value.status_code == 404
    assert manual_context.provider.submissions == []


def test_manual_rejects_hash_already_imported(manual_context):
    task = DownloadTask.create(
        client=manual_context.client,
        remote_id=HASH,
        name="existing",
        state="completed",
        progress=1.0,
        completed_source_ref={"path": "done"},
        import_status="completed",
        media_kind="jav",
        movie="ABC-123",
    )
    DownloadSubmissionRecord.create(
        client_id=manual_context.client.id,
        task_id=task.id,
        movie_number="ABC-123",
        indexer_name="manual",
        title="ABC-123",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
        info_hash=HASH,
        resource_key=f"bt:{HASH}",
    )

    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="jav",
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            movie_number="ABC-123",
        )

    assert error.value.status_code == 409
    assert manual_context.provider.submissions == []


def test_manual_rejects_unsupported_source(manual_context):
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="video",
            source_uri="ftp://example.test/movie.mp4",
        )

    assert error.value.code == "invalid_download_source"
    assert manual_context.provider.submissions == []


def test_manual_accepts_ed2k_and_http_direct_sources(manual_context):
    service = DownloadRequestService()
    ed2k = (
        "ed2k://|file|movie.mp4|12345|"
        "0123456789abcdef0123456789abcdef|/"
    )
    service.create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        source_uri=ed2k,
        source_kind="ed2k",
    )
    ed2k_record = DownloadSubmissionRecord.get(
        DownloadSubmissionRecord.source_kind == "ed2k"
    )
    assert ed2k_record.source_kind == "ed2k"
    assert (
        ed2k_record.resource_key
        == "ed2k:0123456789abcdef0123456789abcdef:12345"
    )
    assert ed2k_record.info_hash == ""
    assert manual_context.provider.submissions[-1].source_kind == "ed2k"
    assert manual_context.provider.submissions[-1].resource_key == ed2k_record.resource_key

    service.create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        source_uri="https://cdn.example/movie.mp4?token=secret",
        source_kind="http",
    )
    http_record = DownloadSubmissionRecord.get(
        DownloadSubmissionRecord.source_kind == "http"
    )
    assert http_record.source_kind == "http"
    assert http_record.resource_key.startswith("http:")
    assert http_record.info_hash == ""
    assert manual_context.provider.submissions[-1].source_kind == "http"
    assert manual_context.provider.submissions[-1].source_uri.startswith(
        "https://cdn.example/"
    )


def test_manual_rejects_declared_source_kind_mismatch(manual_context):
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="video",
            source_uri=f"magnet:?xt=urn:btih:{HASH}",
            source_kind="http",
        )

    assert error.value.code == "invalid_download_source"
    assert manual_context.provider.submissions == []


def test_manual_rejects_source_kind_unsupported_by_client(
    manual_context, monkeypatch
):
    monkeypatch.setattr(
        request_service_module.MEDIA_PROVIDER_REGISTRY,
        "supported_source_kinds",
        lambda _provider_key: {"magnet", "torrent"},
    )

    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_manual_request(
            client_id=manual_context.client.id,
            media_kind="video",
            source_uri=(
                "ed2k://|file|movie.mp4|12345|"
                "0123456789abcdef0123456789abcdef|/"
            ),
            source_kind="ed2k",
        )

    assert error.value.code == "download_source_unsupported"
    assert manual_context.provider.submissions == []


def test_manual_resubmit_keeps_previous_display_name(manual_context):
    service = DownloadRequestService()
    service.create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
        display_name="用户命名",
    )

    service.create_manual_request(
        client_id=manual_context.client.id,
        media_kind="video",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
    )

    task = DownloadTask.get(DownloadTask.remote_id == HASH)
    assert task.display_name == "用户命名"


def test_build_download_import_request_keeps_task_semantics(test_db):
    library = MediaLibrary.create(
        name="import-library", provider_key="demo", provider_config={}
    )
    client = DownloadClient.create(
        name="import-client", library=library, provider_config={}
    )
    video_task = DownloadTask.create(
        client=client,
        remote_id="video",
        name="video",
        state="completed",
        progress=1.0,
        completed_source_ref={"path": "video"},
        import_status="pending",
        media_kind="video",
        collection_id=7,
        delete_source_after_import=True,
    )
    jav_task = DownloadTask.create(
        client=client,
        remote_id="jav",
        name="jav",
        state="completed",
        progress=1.0,
        completed_source_ref={"path": "jav"},
        import_status="pending",
        media_kind="jav",
        movie="ABC-123",
    )

    video_request = ImportTaskService.build_download_import_request(video_task)
    jav_request = ImportTaskService.build_download_import_request(jav_task)

    assert video_request.media_kind == "video"
    assert video_request.collection_id == 7
    assert video_request.source_disposition == "delete_after_commit"
    assert jav_request.media_kind == "jav"
    assert jav_request.collection_id is None
    assert jav_request.source_disposition == "keep"


def test_import_ignores_collection_deleted_before_execution(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="deleted-collection-library", provider_key="demo", provider_config={}
    )
    service = MediaImportService()
    monkeypatch.setattr(
        service,
        "_storage",
        lambda _library: SimpleNamespace(scan_import_source=lambda **_kwargs: ()),
    )

    result = service.import_from_source(
        {"path": "missing"},
        library.id,
        media_kind="video",
        collection_id=9999,
    )

    assert result.imported_count == 0
    assert result.failed_count == 0
