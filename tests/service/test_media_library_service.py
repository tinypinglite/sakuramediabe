from dataclasses import dataclass
from typing import Any

import pytest

from src.api.exception.errors import ApiError
from src.lib.cloud115 import Cloud115AuthError, Cloud115CookieStatus, QrLoginResult
from src.lib.cloud115.types import DirEntry
from src.model import BackgroundTaskRun, DownloadClient, DownloadTask, Image, ImportJob, Media, MediaLibrary, Movie, MovieSeries, VideoItem
from src.model.enums import MediaLibraryBackend
from src.schema.playback.cloud115_libraries import Cloud115LibraryCreateRequest
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryUpdateRequest,
)
from src.service.playback import MediaLibraryService
from src.service.playback import media_library_service as media_library_service_module


@pytest.fixture()
def media_library_tables(test_db):
    models = [Image, MovieSeries, Movie, VideoItem, MediaLibrary, DownloadClient, DownloadTask, BackgroundTaskRun, ImportJob, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


def test_list_libraries_returns_created_at_desc_then_id_desc(media_library_tables):
    first = MediaLibrary.create(
        name="A", backend="local", backend_config={"root_path": "/library/a"}
    )
    second = MediaLibrary.create(
        name="B", backend="local", backend_config={"root_path": "/library/b"}
    )

    items = MediaLibraryService.list_libraries()

    assert [item.id for item in items] == [second.id, first.id]


def test_create_library_strips_and_persists_normalized_fields(media_library_tables):
    resource = MediaLibraryService.create_library(
        MediaLibraryCreateRequest(
            name="  Main  ",
            backend=MediaLibraryBackend.LOCAL,
            backend_config={"root_path": "  /library/main  "},
        )
    )

    assert resource.name == "Main"
    assert resource.backend_config["root_path"] == "/library/main"
    assert MediaLibrary.get_by_id(resource.id).name == "Main"


def test_create_library_rejects_empty_name(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(
                name="   ",
                backend=MediaLibraryBackend.LOCAL,
                backend_config={"root_path": "/library/main"},
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_media_library_name"


def test_create_library_rejects_invalid_root_path(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(
                name="Main",
                backend=MediaLibraryBackend.LOCAL,
                backend_config={"root_path": "relative/path"},
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_media_library_root_path"


def test_create_library_rejects_name_conflict(media_library_tables):
    MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(
                name="Main",
                backend=MediaLibraryBackend.LOCAL,
                backend_config={"root_path": "/library/other"},
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_name_conflict"


def test_create_library_rejects_root_path_conflict(media_library_tables):
    MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(
                name="Other",
                backend=MediaLibraryBackend.LOCAL,
                backend_config={"root_path": "/library/main"},
            )
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_root_path_conflict"


def test_update_library_rejects_empty_payload(media_library_tables):
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(library.id, MediaLibraryUpdateRequest())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_media_library_update"


def test_update_library_rejects_missing_library(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            999,
            MediaLibraryUpdateRequest(name="Updated"),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "media_library_not_found"


def test_update_library_rejects_name_conflict(media_library_tables):
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )
    MediaLibrary.create(
        name="Other", backend="local", backend_config={"root_path": "/library/other"}
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            library.id,
            MediaLibraryUpdateRequest(name="Other"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_name_conflict"


def test_update_library_ignores_unknown_fields_and_yields_empty_update(media_library_tables):
    # root_path 已不再是 update 支持字段，pydantic 会忽略未知键，
    # 于是 update_data 为空 -> 422 empty_media_library_update。
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )
    MediaLibrary.create(
        name="Other", backend="local", backend_config={"root_path": "/library/other"}
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            library.id,
            MediaLibraryUpdateRequest.model_validate({"root_path": "/library/other"}),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_media_library_update"


def test_delete_library_rejects_when_media_exists(media_library_tables):
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")
    Media.create(movie=movie, path="/library/main/video.mp4", library=library)

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"


def test_delete_library_rejects_when_download_client_exists(media_library_tables):
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )
    DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"


def test_delete_library_rejects_when_import_job_exists(media_library_tables):
    library = MediaLibrary.create(
        name="Main", backend="local", backend_config={"root_path": "/library/main"}
    )
    ImportJob.create(source_path="/downloads/a", library=library)

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"


# ---------------------------------------------------------------------------
# create_cloud115_library —— mock 掉 SDK 的 QrLogin / Client
# ---------------------------------------------------------------------------


COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"


@dataclass
class _FakeQrLogin:
    """替换 Cloud115QrLogin，只需要 fetch_result 走通。"""

    result: QrLoginResult | None = None
    exc: Exception | None = None
    calls: list[tuple[str, str]] | None = None  # (uid, app)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def fetch_result(self, uid: str, app: str = "alipaymini") -> QrLoginResult:
        if self.calls is not None:
            self.calls.append((uid, app))
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


class _FakeCloud115Client:
    """替换 Cloud115Client，list_dir/mkdir/check_cookies_alive 全用 fake 数据。"""

    def __init__(
        self,
        *,
        cookies: str,
        alive: bool = True,
        entries_by_offset: dict[int, tuple[list[DirEntry], int]] | None = None,
        mkdir_return: str = "3000000",
    ):
        self.cookies = cookies
        self.user_id = "12345678"
        self._alive = alive
        self._entries_by_offset = entries_by_offset or {0: ([], 0)}
        self._mkdir_return = mkdir_return
        self.mkdir_calls: list[tuple[str, str]] = []

    async def __aenter__(self) -> "_FakeCloud115Client":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def probe_cookies_status(self) -> Cloud115CookieStatus:
        return (
            Cloud115CookieStatus.ALIVE
            if self._alive
            else Cloud115CookieStatus.EXPIRED
        )

    def snapshot_cookies(self) -> str:
        return self.cookies

    async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1000):
        return self._entries_by_offset.get(offset, ([], 0))

    async def mkdir(self, pid: str, name: str) -> str:
        self.mkdir_calls.append((pid, name))
        return self._mkdir_return


def _dir_entry(entry_id: str, name: str) -> DirEntry:
    return DirEntry(
        entry_id=entry_id,
        parent_id="0",
        name=name,
        is_dir=True,
        size=0,
        sha1=None,
        pickcode="",
        mtime=0,
        ctime=0,
        is_video=False,
    )


@pytest.fixture()
def patched_cloud115(monkeypatch):
    """允许每个测试自定义 QrLogin/Client 的行为。用法：patched_cloud115(qr, client_factory)。"""

    def apply(qr: _FakeQrLogin, client_factory):
        monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: qr)
        monkeypatch.setattr(
            media_library_service_module, "Cloud115Client", client_factory
        )

    return apply


async def test_create_cloud115_library_uses_existing_library_root(
    media_library_tables, patched_cloud115
):
    """115 根下已经有 sakuramedia 目录时，直接复用其 cid，不 mkdir。"""
    qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    client = _FakeCloud115Client(
        cookies=COOKIES,
        entries_by_offset={
            0: (
                [
                    _dir_entry("111", "other"),
                    _dir_entry("222", "sakuramedia"),
                ],
                2,
            )
        },
    )
    patched_cloud115(qr, lambda cookies: client)

    resource = await MediaLibraryService.create_cloud115_library(
        Cloud115LibraryCreateRequest(name="115主账号", uid="uid-123")
    )

    assert resource.backend is MediaLibraryBackend.CLOUD115
    assert "cookies" not in resource.backend_config
    assert resource.backend_config["root_cid"] == "222"
    assert resource.backend_config["app"] == "alipaymini"
    assert client.mkdir_calls == []  # 没触发 mkdir
    stored = MediaLibrary.get_by_id(resource.id)
    assert stored.backend_config["cookies"] == COOKIES
    assert stored.backend_account_key == "cloud115:12345678"


async def test_create_cloud115_library_creates_root_when_missing(
    media_library_tables, patched_cloud115
):
    qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    client = _FakeCloud115Client(
        cookies=COOKIES,
        entries_by_offset={0: ([_dir_entry("111", "other")], 1)},
        mkdir_return="cid-new",
    )
    patched_cloud115(qr, lambda cookies: client)

    resource = await MediaLibraryService.create_cloud115_library(
        Cloud115LibraryCreateRequest(name="Cloud", uid="uid-abc")
    )

    assert resource.backend_config["root_cid"] == "cid-new"
    assert client.mkdir_calls == [("0", "sakuramedia")]


async def test_create_cloud115_library_rejects_dead_cookies(
    media_library_tables, patched_cloud115
):
    qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    client = _FakeCloud115Client(cookies=COOKIES, alive=False)
    patched_cloud115(qr, lambda cookies: client)

    with pytest.raises(ApiError) as exc_info:
        await MediaLibraryService.create_cloud115_library(
            Cloud115LibraryCreateRequest(name="X", uid="uid-1")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "cloud115_cookies_invalid"
    # 库不应该落
    assert MediaLibrary.select().count() == 0


async def test_create_cloud115_library_rejects_uid_before_confirmed(
    media_library_tables, patched_cloud115
):
    """扫码尚未 CONFIRMED 时 SDK 抛 Cloud115AuthError → 服务层转 422 cloud115_qrlogin_not_confirmed。"""
    qr = _FakeQrLogin(exc=Cloud115AuthError("not confirmed", endpoint="x"))
    patched_cloud115(qr, lambda cookies: _FakeCloud115Client(cookies=cookies))

    with pytest.raises(ApiError) as exc_info:
        await MediaLibraryService.create_cloud115_library(
            Cloud115LibraryCreateRequest(name="X", uid="uid-1")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "cloud115_qrlogin_not_confirmed"


async def test_create_cloud115_library_rejects_invalid_app(
    media_library_tables, patched_cloud115
):
    """app 不在 APPS 白名单里 → 422，早于任何 SDK 调用。"""
    qr = _FakeQrLogin(exc=AssertionError("should not be called"))
    patched_cloud115(qr, lambda cookies: _FakeCloud115Client(cookies=cookies))

    with pytest.raises(ApiError) as exc_info:
        await MediaLibraryService.create_cloud115_library(
            Cloud115LibraryCreateRequest(name="X", uid="uid-1", app="martian")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_cloud115_app"


async def test_create_cloud115_library_rejects_name_conflict(
    media_library_tables, patched_cloud115
):
    """跟已有本地库同名 → 409，也不动 SDK。"""
    MediaLibrary.create(
        name="dup", backend="local", backend_config={"root_path": "/library/main"}
    )
    qr = _FakeQrLogin(exc=AssertionError("should not be called"))
    patched_cloud115(qr, lambda cookies: _FakeCloud115Client(cookies=cookies))

    with pytest.raises(ApiError) as exc_info:
        await MediaLibraryService.create_cloud115_library(
            Cloud115LibraryCreateRequest(name="dup", uid="uid-1")
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_name_conflict"
