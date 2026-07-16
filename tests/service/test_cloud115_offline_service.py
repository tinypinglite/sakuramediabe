"""Cloud115OfflineDownloadService（离线下载提交）与磁力统一转换的测试。"""

import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.lib.cloud115 import (
    Cloud115OfflineQuotaExceededError,
    Cloud115OfflineTaskExistsError,
    OfflineTask,
    OfflineTaskAddResult,
)
from src.model import DownloadClient, DownloadTask, MediaLibrary
from src.service.transfers import cloud115_offline_service as offline_module
from src.service.transfers.cloud115_offline_service import (
    Cloud115OfflineDownloadService,
    canonicalize_btih,
    resolve_magnet_from_links,
)
from src.service.transfers.qbittorrent_client import QBittorrentClient
from src.schema.transfers.downloads import DownloadCandidateCreatePayload

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"
# 最小合法单文件种子（bencode），libtorrent 可解析出确定的 info_hash。
TORRENT_BYTES = b"d4:infod6:lengthi1e4:name1:a12:piece lengthi16384e6:pieces20:AAAAAAAAAAAAAAAAAAAAee"


@pytest.fixture()
def offline_tables(test_db):
    models = [MediaLibrary, DownloadClient, DownloadTask]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


def _create_cloud115_client_model(name: str = "cloud115-main") -> DownloadClient:
    library = MediaLibrary.create(
        name=f"library-{name}",
        backend="cloud115",
        backend_config={
            "cookies": COOKIES,
            "root_cid": "100",
            "download_root_cid": "dl-root",
        },
    )
    return DownloadClient.create(name=name, kind="cloud115", media_library=library)


def _candidate(**overrides) -> DownloadCandidateCreatePayload:
    payload = {
        "source": "jackett",
        "indexer_name": "mteam",
        "indexer_kind": "pt",
        "title": "ABP-123 4K",
        "size_bytes": 123,
        "seeders": 5,
        "magnet_url": MAGNET,
        "torrent_url": "",
        "tags": [],
    }
    payload.update(overrides)
    return DownloadCandidateCreatePayload.model_validate(payload)


class _FakeCloud115SdkClient:
    """离线提交链路用的假 SDK client：find_or_create_subdir 走真实实现（list_dir + mkdir）。"""

    def __init__(
        self,
        *,
        add_offline_exc: Exception | None = None,
        add_results: list[OfflineTaskAddResult] | None = None,
        remote_tasks: list[OfflineTask] | None = None,
        managed: bool = True,
    ):
        self.mkdir_calls: list[tuple[str, str]] = []
        self.offline_calls: list[tuple[tuple[str, ...], str]] = []
        self._add_offline_exc = add_offline_exc
        self._add_results = add_results
        self._remote_tasks = remote_tasks or []
        self._managed = managed

    async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1150):
        return [], 0

    async def mkdir(self, pid: str, name: str) -> str:
        self.mkdir_calls.append((pid, name))
        return f"cid-{name}"

    async def add_offline_urls(self, urls, *, save_dir_id: str):
        self.offline_calls.append((tuple(urls), save_dir_id))
        if self._add_offline_exc is not None:
            raise self._add_offline_exc
        if self._add_results is not None:
            return self._add_results
        return [OfflineTaskAddResult(info_hash="a" * 40, url=urls[0])]

    async def list_offline_tasks(self, *, page: int, page_size: int):
        return SimpleNamespace(
            page=page,
            page_count=1,
            page_size=page_size,
            total_tasks=len(self._remote_tasks),
            tasks=self._remote_tasks if page == 1 else [],
        )

    async def dir_info(self, cid: str):
        paths = [SimpleNamespace(file_id="dl-root")] if self._managed else []
        return SimpleNamespace(cid=cid, paths=paths)


def _remote_task(*, info_hash: str = "a" * 40, save_dir_id: str = "real-cid") -> OfflineTask:
    return OfflineTask(
        info_hash=info_hash,
        name="ABP-123",
        size=0,
        status=1,
        status_text="",
        percent_done=0,
        rate_download=0,
        peers=0,
        left_time_seconds=0,
        add_time=0,
        last_update=0,
        file_id="",
        pickcode="",
        save_dir_id=save_dir_id,
        source_url="",
        retry_count=0,
        retry_limit=0,
    )


@pytest.fixture()
def patched_offline_sdk(monkeypatch):
    """把 cloud115_client_for / ensure_download_root_cid 替换成假实现，返回 fake client。"""

    def apply(fake: _FakeCloud115SdkClient):
        @asynccontextmanager
        async def _fake_client_for(library):
            yield fake

        async def _fake_ensure(library, client):
            return library.backend_config["download_root_cid"]

        monkeypatch.setattr(offline_module, "cloud115_client_for", _fake_client_for)
        monkeypatch.setattr(offline_module, "ensure_download_root_cid", _fake_ensure)
        return fake

    return apply


# ---------------------------------------------------------------------------
# resolve_magnet_from_links —— 磁力统一入口
# ---------------------------------------------------------------------------


def test_resolve_magnet_passes_through_magnet_url():
    magnet, info_hash = resolve_magnet_from_links(MAGNET, "")
    assert magnet == MAGNET
    assert info_hash == "a" * 40


def test_canonicalize_btih_accepts_hex_and_base32():
    expected = "ab" * 20
    base32_value = base64.b32encode(bytes.fromhex(expected)).decode("ascii")

    assert canonicalize_btih(expected.upper()) == expected
    assert canonicalize_btih(base32_value.lower()) == expected


@pytest.mark.parametrize("value", ["", "a" * 39, "0" * 32, "not-a-hash"])
def test_canonicalize_btih_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        canonicalize_btih(value)


def test_resolve_magnet_detects_magnet_inside_torrent_url_field():
    # 索引器会把磁力塞进 torrent_url 字段：按内容而非字段名分流。
    magnet, info_hash = resolve_magnet_from_links("", MAGNET)
    assert magnet == MAGNET
    assert info_hash == "a" * 40


def test_resolve_magnet_converts_torrent_url_to_magnet():
    expected_hash = QBittorrentClient.parse_hash_from_torrent(TORRENT_BYTES)

    class _FakeResponse:
        content = TORRENT_BYTES

        def raise_for_status(self):
            pass

    class _FakeHttpClient:
        def get(self, url):
            assert url == "https://example.com/file.torrent"
            return _FakeResponse()

    magnet, info_hash = resolve_magnet_from_links(
        "", "https://example.com/file.torrent", http_client=_FakeHttpClient()
    )
    assert info_hash == expected_hash
    assert magnet == f"magnet:?xt=urn:btih:{expected_hash}"


def test_resolve_magnet_rejects_empty_links():
    with pytest.raises(ApiError) as exc_info:
        resolve_magnet_from_links("", "")
    assert exc_info.value.code == "invalid_download_request_candidate"


def test_resolve_magnet_rejects_unparseable_torrent_bytes():
    class _FakeResponse:
        content = b"not a torrent"

        def raise_for_status(self):
            pass

    class _FakeHttpClient:
        def get(self, url):
            return _FakeResponse()

    with pytest.raises(ApiError) as exc_info:
        resolve_magnet_from_links(
            "", "https://example.com/bad.torrent", http_client=_FakeHttpClient()
        )
    assert exc_info.value.code == "invalid_download_request_candidate"


# ---------------------------------------------------------------------------
# submit_candidate —— 提交与登记
# ---------------------------------------------------------------------------


def test_submit_candidate_creates_task_with_target_ref(offline_tables, patched_offline_sdk):
    fake = patched_offline_sdk(_FakeCloud115SdkClient())
    client_model = _create_cloud115_client_model()

    task, created = Cloud115OfflineDownloadService().submit_candidate(
        client_model,
        movie_number="ABP-123",
        candidate=_candidate(),
    )

    assert created is True
    # 完整 canonical hash 目录建在下载缓冲根之下，磁力原样提交。
    assert fake.mkdir_calls == [("dl-root", "a" * 40)]
    assert fake.offline_calls == [((MAGNET,), "cid-" + "a" * 40)]
    assert task.movie == "ABP-123"
    assert task.info_hash == "a" * 40
    assert task.save_path == "sakuramedia_downloads/" + "a" * 40
    assert task.target_ref == {"cid": "cid-" + "a" * 40}
    assert task.download_state == "queued"
    assert task.import_status == "pending"


def test_submit_candidate_reuses_existing_local_task_without_sdk_call(
    offline_tables, patched_offline_sdk
):
    fake = patched_offline_sdk(_FakeCloud115SdkClient())
    client_model = _create_cloud115_client_model()
    existing = DownloadTask.create(
        client=client_model,
        movie="ABP-123",
        name="ABP-123",
        info_hash="a" * 40,
        save_path="sakuramedia_downloads/ABP-123",
        target_ref={"cid": "cid-ABP-123"},
        download_state="downloading",
        import_status="pending",
    )

    task, created = Cloud115OfflineDownloadService().submit_candidate(
        client_model,
        movie_number="ABP-123",
        candidate=_candidate(),
    )

    assert created is False
    assert task.id == existing.id
    assert fake.offline_calls == []  # 幂等：没有重复提交 115


def test_submit_candidate_registers_local_task_when_remote_already_exists(
    offline_tables, patched_offline_sdk
):
    # 115 侧已有同 info_hash 任务（本地记录曾丢失）：不视为失败，登记本地镜像。
    fake = patched_offline_sdk(
        _FakeCloud115SdkClient(
            add_offline_exc=Cloud115OfflineTaskExistsError("exists"),
            remote_tasks=[_remote_task(save_dir_id="real-cid")],
        )
    )
    client_model = _create_cloud115_client_model()

    task, created = Cloud115OfflineDownloadService().submit_candidate(
        client_model,
        movie_number="ABP-123",
        candidate=_candidate(),
    )

    assert created is True
    assert task.target_ref == {"cid": "real-cid"}


def test_submit_candidate_rejects_unmanaged_existing_remote_task(
    offline_tables, patched_offline_sdk
):
    patched_offline_sdk(
        _FakeCloud115SdkClient(
            add_offline_exc=Cloud115OfflineTaskExistsError("exists"),
            remote_tasks=[_remote_task(save_dir_id="user-cid")],
            managed=False,
        )
    )
    client_model = _create_cloud115_client_model()

    with pytest.raises(ApiError) as exc_info:
        Cloud115OfflineDownloadService().submit_candidate(
            client_model, movie_number="ABP-123", candidate=_candidate()
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "cloud115_offline_task_exists_unmanaged"
    assert DownloadTask.select().count() == 0


@pytest.mark.parametrize(
    ("results", "expected_code"),
    [
        ([], "cloud115_offline_submit_invalid_response"),
        ([OfflineTaskAddResult(info_hash="", url=MAGNET)], "cloud115_offline_submit_invalid_response"),
        ([OfflineTaskAddResult(info_hash="b" * 40, url=MAGNET)], "cloud115_offline_submit_hash_mismatch"),
    ],
)
def test_submit_candidate_validates_single_submit_result(
    offline_tables, patched_offline_sdk, results, expected_code
):
    patched_offline_sdk(_FakeCloud115SdkClient(add_results=results))
    client_model = _create_cloud115_client_model()

    with pytest.raises(ApiError) as exc_info:
        Cloud115OfflineDownloadService().submit_candidate(
            client_model, movie_number="ABP-123", candidate=_candidate()
        )

    assert exc_info.value.code == expected_code
    assert DownloadTask.select().count() == 0


def test_submit_candidate_reports_quota_exhausted_without_fallback(
    offline_tables, patched_offline_sdk
):
    patched_offline_sdk(
        _FakeCloud115SdkClient(add_offline_exc=Cloud115OfflineQuotaExceededError("quota"))
    )
    client_model = _create_cloud115_client_model()

    with pytest.raises(ApiError) as exc_info:
        Cloud115OfflineDownloadService().submit_candidate(
            client_model,
            movie_number="ABP-123",
            candidate=_candidate(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "cloud115_offline_quota_exceeded"
    # 提交失败不留半截本地任务。
    assert DownloadTask.select().count() == 0
