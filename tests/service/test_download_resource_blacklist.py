import base64
import hashlib
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.api.exception.errors import ApiError
from src.model import (
    DownloadClient,
    DownloadResourceBlacklist,
    DownloadSubmissionRecord,
    DownloadTask,
    MediaLibrary,
)
from src.plugins.provider_protocol import ProviderOperationError, RemoteDownloadTask
from src.schema.transfers.downloads import DownloadRequestCreateRequest
from src.service.transfers.downloads import resource_hash
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.task_service import DownloadTaskService
from src.service.transfers.shared.import_task_service import ImportTaskService

HASH = "0123456789abcdef0123456789abcdef01234567"
OTHER_HASH = "abcdef0123456789abcdef0123456789abcdef01"
BASE32_HASH = base64.b32encode(bytes.fromhex(HASH)).decode()
TORRENT_INFO = (
    b"d6:lengthi1e4:name8:test.mkv12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"
)
TORRENT = b"d4:info" + TORRENT_INFO + b"e"


@pytest.mark.parametrize(
    "uri",
    [
        f"magnet:?xt=urn:btih:{HASH}",
        f"magnet:?dn=another-name&xt=urn:btih:{HASH.upper()}&tr=https://tracker.test",
        f"magnet:?xt=urn:btih:{BASE32_HASH.lower()}",
        f"magnet://?xt=urn%3Abtih%3A{HASH}",
    ],
)
def test_magnet_variants_have_same_hash(uri):
    assert resource_hash.resolve_resource_hash(uri) == HASH


def _http(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(
        resource_hash.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_torrent_hash_matches_info_dictionary(monkeypatch):
    _http(monkeypatch, lambda request: httpx.Response(200, content=TORRENT))
    assert (
        resource_hash.resolve_resource_hash("https://indexer.test/file")
        == hashlib.sha1(TORRENT_INFO).hexdigest()
    )


def test_http_redirect_to_magnet(monkeypatch):
    _http(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": f"magnet:?xt=urn:btih:{HASH}"}
        ),
    )
    assert resource_hash.resolve_resource_hash("https://indexer.test/file") == HASH


def test_relative_http_redirect(monkeypatch):
    requests = []

    def handle(request):
        requests.append(str(request.url))
        if request.url.path == "/entry":
            return httpx.Response(302, headers={"location": "/file"})
        return httpx.Response(200, content=TORRENT)

    _http(monkeypatch, handle)
    assert (
        resource_hash.resolve_resource_hash("https://indexer.test/entry")
        == hashlib.sha1(TORRENT_INFO).hexdigest()
    )
    assert requests == ["https://indexer.test/entry", "https://indexer.test/file"]


@pytest.mark.parametrize("status,expected", [(404, 404), (403, 422), (500, 503)])
def test_http_failures_are_safe_api_errors(monkeypatch, status, expected):
    _http(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(ApiError) as error:
        resource_hash.resolve_resource_hash("https://indexer.test/file?apikey=private")
    assert error.value.status_code == expected
    assert "private" not in str(error.value)


def test_torrent_size_limit(monkeypatch):
    monkeypatch.setattr(resource_hash, "MAX_TORRENT_BYTES", 5)
    _http(monkeypatch, lambda request: httpx.Response(200, content=b"123456"))
    with pytest.raises(ApiError, match="大小限制"):
        resource_hash.resolve_resource_hash("https://indexer.test/file")


def test_redirect_limit(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/again"})

    _http(monkeypatch, handle)
    with pytest.raises(ApiError, match="重定向次数"):
        resource_hash.resolve_resource_hash("https://indexer.test/file")
    assert len(requests) == resource_hash.MAX_HTTP_REDIRECTS + 1


def test_invalid_torrent(monkeypatch):
    _http(
        monkeypatch, lambda request: httpx.Response(200, content=b"<html>login</html>")
    )
    with pytest.raises(ApiError, match="种子文件无效"):
        resource_hash.resolve_resource_hash("https://indexer.test/file")


@pytest.fixture
def downloads(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="local", provider_key="local", provider_config={}
    )
    client = DownloadClient.create(name="qB", library=library, provider_config={})
    provider = SimpleNamespace(
        submit=Mock(
            return_value=RemoteDownloadTask(
                remote_id=HASH,
                name="TEST-001",
                state="queued",
                progress=0,
                completed_source_ref=None,
            )
        ),
        delete_task=Mock(),
    )
    monkeypatch.setattr(
        DownloadRequestService, "_resolve_client", staticmethod(lambda payload: client)
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.request_service.download_provider",
        lambda client: provider,
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.task_service.download_provider",
        lambda client: provider,
    )
    return client, provider


def _payload(info_hash=HASH):
    return DownloadRequestCreateRequest(
        movie_number="TEST-001",
        candidate={
            "source_uri": f"magnet:?xt=urn:btih:{info_hash}",
            "indexer_name": "indexer",
            "title": "TEST-001",
            "size_bytes": 1024**3,
            "seeders": 10,
        },
    )


def _task(client, status, info_hash=HASH):
    return DownloadTask.create(
        client=client,
        movie="TEST-001",
        name="TEST-001",
        remote_id=info_hash,
        state="completed",
        import_status=status,
    )


@pytest.mark.parametrize(
    "status,blocked",
    [("failed", True), ("skipped", True), ("pending", False), ("completed", False)],
)
@pytest.mark.parametrize("delete_files", [True, False])
def test_only_failed_and_skipped_deletions_blacklist_old_tasks(
    downloads, status, blocked, delete_files
):
    client, provider = downloads
    task = _task(client, status, HASH.upper())
    DownloadTaskService.delete_task(task.id, delete_files=delete_files)
    assert DownloadTask.get_or_none(task.id) is None
    assert DownloadResourceBlacklist.select().exists() is blocked
    if blocked:
        assert DownloadResourceBlacklist.get().info_hash == HASH
    provider.delete_task.assert_called_once_with(
        remote_id=HASH.upper(), delete_files=delete_files
    )


def test_running_import_cannot_be_deleted(downloads):
    client, provider = downloads
    task = _task(client, "running")
    with pytest.raises(ApiError) as error:
        DownloadTaskService.delete_task(task.id, delete_files=True)
    assert error.value.status_code == 409
    provider.delete_task.assert_not_called()
    assert DownloadTask.get_or_none(task.id) is not None
    assert not DownloadResourceBlacklist.select().exists()


@pytest.mark.parametrize(
    "code,deleted", [("unavailable", False), ("source_not_found", True)]
)
def test_remote_delete_errors(downloads, code, deleted):
    client, provider = downloads
    task = _task(client, "failed")
    provider.delete_task.side_effect = ProviderOperationError(
        provider_key="local",
        operation="delete_task",
        code=code,
        safe_message="删除失败",
        retryable=False,
    )
    if deleted:
        DownloadTaskService.delete_task(task.id, delete_files=True)
    else:
        with pytest.raises(ApiError):
            DownloadTaskService.delete_task(task.id, delete_files=True)
    assert (DownloadTask.get_or_none(task.id) is None) is deleted
    assert DownloadResourceBlacklist.select().exists() is deleted


def test_blacklist_insert_and_local_delete_are_atomic(downloads, monkeypatch):
    client, provider = downloads
    task = _task(client, "failed")
    monkeypatch.setattr(
        DownloadTask, "delete_instance", Mock(side_effect=RuntimeError("db error"))
    )
    with pytest.raises(RuntimeError):
        DownloadTaskService.delete_task(task.id, delete_files=True)
    provider.delete_task.assert_called_once()
    assert DownloadTask.get_or_none(task.id) is not None
    assert not DownloadResourceBlacklist.select().exists()


def test_repeated_hash_deletion_is_idempotent(downloads):
    client, _provider = downloads
    for _ in range(2):
        task = _task(client, "skipped")
        DownloadTaskService.delete_task(task.id, delete_files=True)
    assert DownloadResourceBlacklist.select().count() == 1


def test_submit_delete_and_cross_client_resubmit_cycle(downloads, monkeypatch):
    client, provider = downloads
    response = DownloadRequestService().create_request(_payload())
    record = DownloadSubmissionRecord.get()
    assert (record.info_hash, record.state, record.remote_id, record.task_id) == (
        HASH,
        "submitted",
        HASH,
        response.task.id,
    )
    assert record.source_uri == _payload().candidate.source_uri
    task = DownloadTask.get_by_id(response.task.id)
    task.import_status = "failed"
    task.save()
    DownloadTaskService.delete_task(task.id, delete_files=True)
    client.delete_instance()
    assert DownloadSubmissionRecord.select().count() == 1

    library = MediaLibrary.create(
        name="cloud", provider_key="cloud115", provider_config={}
    )
    other_client = DownloadClient.create(
        name="115", library=library, provider_config={}
    )
    monkeypatch.setattr(
        DownloadRequestService,
        "_resolve_client",
        staticmethod(lambda payload: other_client),
    )
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_request(_payload(BASE32_HASH))
    assert error.value.code == "download_source_blacklisted"
    assert provider.submit.call_count == 1
    assert DownloadSubmissionRecord.select().count() == 1
    provider.submit.return_value = RemoteDownloadTask(
        remote_id=OTHER_HASH,
        name="TEST-001",
        state="queued",
        progress=0,
        completed_source_ref=None,
    )
    assert DownloadRequestService().create_request(_payload(OTHER_HASH)).created
    assert provider.submit.call_count == 2


def test_each_submission_is_recorded_when_provider_returns_existing_task(downloads):
    first = DownloadRequestService().create_request(_payload())
    second = DownloadRequestService().create_request(_payload())
    assert first.created and not second.created
    assert first.task.id == second.task.id
    assert DownloadSubmissionRecord.select().count() == 2
    assert {record.task_id for record in DownloadSubmissionRecord.select()} == {
        first.task.id
    }


def test_submission_failure_is_recorded(downloads):
    _client, provider = downloads
    provider.submit.side_effect = ProviderOperationError(
        provider_key="local",
        operation="submit",
        code="unavailable",
        safe_message="不可用",
        retryable=True,
    )
    with pytest.raises(ApiError):
        DownloadRequestService().create_request(_payload())
    record = DownloadSubmissionRecord.get()
    assert record.state == "failed"
    assert record.error_code == "unavailable"
    assert record.task_id is None
    assert not DownloadTask.select().exists()
    assert not DownloadResourceBlacklist.select().exists()


def test_unparseable_source_never_reaches_provider(downloads):
    _client, provider = downloads
    with pytest.raises(ApiError):
        DownloadRequestService().create_request(_payload("invalid"))
    provider.submit.assert_not_called()
    assert not DownloadSubmissionRecord.select().exists()


def test_automatic_cleanup_does_not_blacklist(downloads, monkeypatch):
    client, provider = downloads
    task = _task(client, "completed")
    monkeypatch.setattr(
        "src.service.transfers.shared.import_task_service.download_provider",
        lambda client: provider,
    )
    ImportTaskService._delete_remote_download_task(task.id)
    provider.delete_task.assert_called_once_with(remote_id=HASH, delete_files=False)
    assert not DownloadResourceBlacklist.select().exists()


def test_migration_creates_history_without_changing_download_tasks(test_db, downloads):
    client, _provider = downloads
    task = _task(client, "failed")
    test_db.drop_tables([DownloadSubmissionRecord, DownloadResourceBlacklist])
    migration = import_module(
        "src.start.migrations.versions.20260908_01_add_download_resource_history"
    )
    migration.migrate(test_db)
    record = DownloadSubmissionRecord.create(
        client_id=client.id,
        task_id=task.id,
        movie_number="TEST-001",
        title="TEST-001",
        indexer_name="indexer",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
        info_hash=HASH,
    )
    DownloadResourceBlacklist.create(info_hash=HASH)
    migration.migrate(test_db)
    assert DownloadSubmissionRecord.get_by_id(record.id).info_hash == HASH
    assert DownloadTask.get_by_id(task.id).import_status == "failed"
    assert DownloadResourceBlacklist.select().count() == 1
    index = next(
        index
        for index in test_db.get_indexes("download_resource_blacklist")
        if index.columns == ["info_hash"]
    )
    assert index.unique


def test_network_timeout_never_reaches_provider(downloads, monkeypatch):
    _client, provider = downloads

    def handle(request):
        raise httpx.ReadTimeout("private source URL", request=request)

    _http(monkeypatch, handle)
    payload = _payload()
    payload.candidate.source_uri = "https://indexer.test/file?apikey=private"
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_request(payload)
    assert error.value.status_code == 503
    assert "private" not in str(error.value)
    provider.submit.assert_not_called()


def test_blacklisted_torrent_url_never_reaches_provider(downloads, monkeypatch):
    _client, provider = downloads
    DownloadResourceBlacklist.create(info_hash=hashlib.sha1(TORRENT_INFO).hexdigest())
    _http(monkeypatch, lambda request: httpx.Response(200, content=TORRENT))
    payload = _payload()
    payload.candidate.source_uri = "https://another-indexer.test/new-url"
    with pytest.raises(ApiError) as error:
        DownloadRequestService().create_request(payload)
    assert error.value.code == "download_source_blacklisted"
    provider.submit.assert_not_called()


def test_delete_uses_recorded_hash_instead_of_opaque_remote_id(downloads):
    _client, provider = downloads
    provider.submit.return_value = RemoteDownloadTask(
        remote_id="opaque-id",
        name="TEST-001",
        state="queued",
        progress=0,
        completed_source_ref=None,
    )
    response = DownloadRequestService().create_request(_payload())
    task = DownloadTask.get_by_id(response.task.id)
    task.import_status = "skipped"
    task.save()
    DownloadTaskService.delete_task(task.id, delete_files=True)
    assert DownloadResourceBlacklist.get().info_hash == HASH
    assert DownloadSubmissionRecord.get().remote_id == "opaque-id"


def test_unknown_legacy_hash_is_rejected_before_remote_deletion(downloads):
    client, provider = downloads
    task = _task(client, "failed", "not-a-hash")
    with pytest.raises(ApiError) as error:
        DownloadTaskService.delete_task(task.id, delete_files=True)
    assert error.value.code == "invalid_download_resource_hash"
    provider.delete_task.assert_not_called()
    assert DownloadTask.get_or_none(task.id) is not None


def test_unexpected_submit_failure_keeps_history(downloads):
    _client, provider = downloads
    provider.submit.side_effect = RuntimeError("provider failed")
    with pytest.raises(RuntimeError):
        DownloadRequestService().create_request(_payload())
    record = DownloadSubmissionRecord.get()
    assert record.state == "failed"
    assert record.error_code == "RuntimeError"
