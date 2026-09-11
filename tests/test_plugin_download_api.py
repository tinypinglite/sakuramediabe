"""插件下载门面契约：目标下载器固定且提交沿用宿主请求链路。"""

from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import DownloadClient, MediaLibrary
from src.plugins import (
    PluginContext,
    PluginDownloadCandidate,
    PluginDownloadResult,
    PluginDownloadTarget,
)
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.search_service import DownloadSearchService


def _client(name: str = "qBittorrent"):
    library = MediaLibrary.create(
        name=f"{name} 媒体库",
        provider_key="local",
        provider_config={},
    )
    return DownloadClient.create(name=name, library=library, provider_config={})


def _raw_candidate(client: DownloadClient) -> DownloadCandidateResource:
    return DownloadCandidateResource(
        source_uri="magnet:?xt=urn:btih:ABCDEF",
        indexer_name="indexer",
        indexer_kind="pt",
        resolved_client_id=client.id,
        resolved_client_name=client.name,
        download_clients=[{"id": client.id, "name": client.name}],
        movie_number="ABC-001",
        title="ABC-001 1080p",
        size_bytes=1024,
        seeders=8,
    )


def test_search_candidates_are_bound_to_requested_client(test_db, tmp_path, monkeypatch):
    client = _client()
    calls = []

    def fake_search(self, **kwargs):
        calls.append(kwargs)
        return [_raw_candidate(client)]

    monkeypatch.setattr(DownloadSearchService, "search_candidates", fake_search)

    downloads = PluginContext("download_demo", {}, tmp_path).downloads
    target = downloads.get_target(client.id)
    assert target == PluginDownloadTarget(
        download_client_id=client.id,
        download_client_name=client.name,
        library_id=client.library_id,
        library_name=client.library.name,
        provider_key="local",
    )

    candidates = downloads.search_candidates(
        movie_number="abc-001",
        download_client_id=client.id,
        indexer_kind="pt",
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.download_client_id == client.id
    assert candidate.library_id == client.library_id
    assert candidate.library_name == client.library.name
    assert candidate.provider_key == "local"
    assert calls == [{
        "movie_number": "abc-001",
        "indexer_kind": "pt",
        "download_client_id": client.id,
    }]


def test_submit_reuses_host_request_service_with_explicit_client(
    test_db,
    tmp_path,
    monkeypatch,
):
    client = _client()
    candidate = PluginDownloadCandidate(
        source_uri="magnet:?xt=urn:btih:ABCDEF",
        indexer_name="indexer",
        indexer_kind="pt",
        download_client_id=client.id,
        download_client_name=client.name,
        library_id=client.library_id,
        library_name=client.library.name,
        provider_key="local",
        movie_number="ABC-001",
        title="ABC-001 1080p",
        size_bytes=1024,
        seeders=8,
    )
    captured = {}

    def fake_create_request(self, payload):
        captured["payload"] = payload
        return SimpleNamespace(task=SimpleNamespace(id=42), created=True)

    monkeypatch.setattr(DownloadRequestService, "create_request", fake_create_request)

    result = PluginContext("download_demo", {}, tmp_path).downloads.submit(
        movie_number="abc-001",
        candidate=candidate,
    )

    assert result == PluginDownloadResult(task_id=42, created=True)
    assert captured["payload"].client_id == client.id
    assert captured["payload"].movie_number == "ABC-001"
    assert captured["payload"].candidate.source_uri == candidate.source_uri


def test_submit_rejects_stale_library_target(test_db, tmp_path, monkeypatch):
    client = _client()
    candidate = PluginDownloadCandidate(
        source_uri="magnet:?xt=urn:btih:ABCDEF",
        indexer_name="indexer",
        indexer_kind="pt",
        download_client_id=client.id,
        download_client_name=client.name,
        library_id=client.library_id,
        library_name=client.library.name,
        provider_key="different-provider",
        movie_number="ABC-001",
        title="ABC-001",
        size_bytes=1,
        seeders=1,
    )
    called = False

    def fake_create_request(self, payload):
        del self, payload
        nonlocal called
        called = True
        raise AssertionError("stale candidate must not reach request service")

    monkeypatch.setattr(DownloadRequestService, "create_request", fake_create_request)

    with pytest.raises(ApiError) as caught:
        PluginContext("download_demo", {}, tmp_path).downloads.submit(
            movie_number="ABC-001",
            candidate=candidate,
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "plugin_download_candidate_stale"
    assert called is False


def test_list_targets_returns_all_configured_clients(test_db, tmp_path):
    downloads = PluginContext("download_demo", {}, tmp_path).downloads
    assert downloads.list_targets() == ()
    first = _client("first")
    second = _client("second")
    assert downloads.list_targets() == (
        downloads.get_target(first.id),
        downloads.get_target(second.id),
    )


def test_search_without_client_preserves_each_host_target(test_db, tmp_path, monkeypatch):
    first = _client("first")
    second = _client("second")
    calls = []

    def fake_search(self, **kwargs):
        calls.append(kwargs)
        return [_raw_candidate(second), _raw_candidate(first), _raw_candidate(second)]

    monkeypatch.setattr(DownloadSearchService, "search_candidates", fake_search)
    candidates = PluginContext("download_demo", {}, tmp_path).downloads.search_candidates(
        movie_number="ABC-001",
    )
    assert [(item.download_client_id, item.library_id) for item in candidates] == [
        (second.id, second.library_id),
        (first.id, first.library_id),
        (second.id, second.library_id),
    ]
    assert calls == [{
        "movie_number": "ABC-001", "indexer_kind": None, "download_client_id": None,
    }]


def test_search_without_client_returns_empty_results(tmp_path, monkeypatch):
    monkeypatch.setattr(DownloadSearchService, "search_candidates", lambda self, **kwargs: [])
    assert PluginContext("download_demo", {}, tmp_path).downloads.search_candidates(
        movie_number="ABC-001",
    ) == ()


@pytest.mark.parametrize("client_id", [0, -1, True, "1"])
def test_search_rejects_invalid_explicit_client_id(tmp_path, client_id):
    with pytest.raises(ValueError, match="download_client_id"):
        PluginContext("download_demo", {}, tmp_path).downloads.search_candidates(
            movie_number="ABC-001", download_client_id=client_id,
        )


def test_search_rejects_host_target_mismatch(test_db, tmp_path, monkeypatch):
    first = _client("first")
    second = _client("second")
    monkeypatch.setattr(
        DownloadSearchService, "search_candidates", lambda self, **kwargs: [_raw_candidate(second)],
    )
    with pytest.raises(ApiError) as caught:
        PluginContext("download_demo", {}, tmp_path).downloads.search_candidates(
            movie_number="ABC-001", download_client_id=first.id,
        )
    assert caught.value.code == "plugin_download_candidate_target_mismatch"
