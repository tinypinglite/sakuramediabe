"""cloud115 后端接入层测试：client ctx 快照回写 + keepalive 探活与失效通知。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.lib.cloud115 import Cloud115Client, Cloud115CookieStatus
from src.model import (
    BackgroundTaskRun,
    Image,
    MediaLibrary,
    SystemEvent,
    SystemNotification,
)
from src.service.playback.cloud115_backend_service import (
    Cloud115KeepaliveService,
    cloud115_client_for,
    require_cloud115_library,
)

COOKIE = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz; KID=kkk"
REAUTH_COOKIE = "UID=12345678_A1_1800000000; CID=new; SEID=new"


@pytest.fixture()
def cloud115_tables(test_db):
    models = [Image, MediaLibrary, BackgroundTaskRun, SystemEvent, SystemNotification]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_cloud_library(name: str = "cloud", cookies: str = COOKIE) -> MediaLibrary:
    return MediaLibrary.create(
        name=name,
        backend="cloud115",
        backend_config={"cookies": cookies, "root_cid": "3000000", "app": "alipaymini"},
    )


def test_require_cloud115_library_rejects_local_backend(cloud115_tables):
    library = MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library/a"}
    )
    with pytest.raises(ValueError, match="expected cloud115"):
        require_cloud115_library(library)


def test_require_cloud115_library_rejects_incomplete_config(cloud115_tables):
    library = MediaLibrary.create(
        name="broken", backend="cloud115", backend_config={"cookies": COOKIE}
    )
    with pytest.raises(ValueError, match="root_cid"):
        require_cloud115_library(library)


def test_cloud115_client_for_persists_refreshed_cookies(cloud115_tables, monkeypatch):
    """SDK 内 cookies 被服务端 Set-Cookie 刷新后，退出 ctx 时应回写 backend_config。"""
    library = _create_cloud_library()

    async def _use():
        async with cloud115_client_for(library) as client:
            # 模拟服务端塞回新 acw_tc（真实场景由 _merge_set_cookies 完成）
            client._cookies_dict["acw_tc"] = "fresh-token"

    asyncio.run(_use())

    fresh = MediaLibrary.get_by_id(library.id)
    assert "acw_tc=fresh-token" in fresh.backend_config["cookies"]
    # 其它配置键不能被覆盖
    assert fresh.backend_config["root_cid"] == "3000000"
    assert fresh.backend_config["app"] == "alipaymini"


def test_cloud115_client_for_skips_write_when_cookies_unchanged(cloud115_tables):
    library = _create_cloud_library()
    before_updated_at = MediaLibrary.get_by_id(library.id).updated_at

    async def _use():
        async with cloud115_client_for(library) as _client:
            pass

    asyncio.run(_use())
    after = MediaLibrary.get_by_id(library.id)
    assert after.updated_at == before_updated_at
    assert after.backend_config["cookies"] == COOKIE


def test_cloud115_client_for_does_not_overwrite_concurrent_reauth(
    cloud115_tables,
):
    """旧 context 退出时不得把 reauth 已写入的新 cookies/app 回滚。"""
    library = _create_cloud_library()

    async def _use():
        async with cloud115_client_for(library) as client:
            client._cookies_dict["acw_tc"] = "old-context-refresh"
            fresh = MediaLibrary.get_by_id(library.id)
            config = dict(fresh.backend_config)
            config["cookies"] = REAUTH_COOKIE
            config["app"] = "web"
            fresh.backend_config = config
            fresh.save(only=[MediaLibrary.backend_config])

    asyncio.run(_use())

    after = MediaLibrary.get_by_id(library.id)
    assert after.backend_config["cookies"] == REAUTH_COOKIE
    assert after.backend_config["app"] == "web"
    assert "old-context-refresh" not in after.backend_config["cookies"]


def test_keepalive_counts_alive_and_notifies_expired_once(cloud115_tables, monkeypatch):
    """失效库发通知；存在未读同题通知时不重复发。"""
    _create_cloud_library(name="alive-lib")
    _create_cloud_library(name="dead-lib")

    async def fake_check(cls, library):
        if library.name == "alive-lib":
            return Cloud115CookieStatus.ALIVE
        return Cloud115CookieStatus.EXPIRED

    monkeypatch.setattr(
        Cloud115KeepaliveService, "_check_library", classmethod(fake_check)
    )

    stats = Cloud115KeepaliveService.run()
    assert stats == {"total": 2, "alive": 1, "expired": 1, "unavailable": 0}
    notifications = list(SystemNotification.select())
    assert len(notifications) == 1
    assert "dead-lib" in notifications[0].title
    assert notifications[0].category == "warning"
    assert notifications[0].related_resource_type == "media_library"

    # 再跑一轮：未读通知仍在，不应新增
    stats = Cloud115KeepaliveService.run()
    assert stats["expired"] == 1
    assert SystemNotification.select().count() == 1


def test_keepalive_skips_when_no_cloud115_library(cloud115_tables):
    MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library/a"}
    )
    stats = Cloud115KeepaliveService.run()
    assert stats == {"total": 0, "alive": 0, "expired": 0, "unavailable": 0}


def test_keepalive_does_not_notify_on_temporary_upstream_failure(
    cloud115_tables, monkeypatch
):
    _create_cloud_library(name="temporary-down")

    async def fake_check(cls, library):
        return Cloud115CookieStatus.UNAVAILABLE

    monkeypatch.setattr(
        Cloud115KeepaliveService, "_check_library", classmethod(fake_check)
    )

    stats = Cloud115KeepaliveService.run()

    assert stats == {"total": 1, "alive": 0, "expired": 0, "unavailable": 1}
    assert SystemNotification.select().count() == 0


# ---------------------------------------------------------------------------
# assert_cid_outside_library_root：导入源与管理目录互不包含的防御校验
# ---------------------------------------------------------------------------


class _FakeDirInfoClient:
    """dir_info 替身：用预置的 cid -> 面包屑链模拟目录层级。"""

    def __init__(self, paths_by_cid: dict[str, tuple[str, ...]]):
        # value 是该目录的祖先 cid 链（从根到父级）
        self._paths_by_cid = paths_by_cid

    async def dir_info(self, cid: str):
        from src.lib.cloud115.types import DirBreadcrumb, DirMeta

        crumbs = tuple(
            DirBreadcrumb(file_id=ancestor, name=f"dir-{ancestor}")
            for ancestor in self._paths_by_cid.get(cid, ())
        )
        return DirMeta(
            cid=cid, name=f"dir-{cid}", pickcode="", parent_id="",
            file_count=0, folder_count=0, play_long_seconds=0,
            mtime=0, ctime=0, paths=crumbs,
        )


def test_assert_cid_rejects_source_equal_to_root():
    from src.api.exception.errors import ApiError
    from src.service.playback.cloud115_backend_service import (
        assert_cid_outside_library_root,
    )

    client = _FakeDirInfoClient({})
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            assert_cid_outside_library_root(client, source_cid="R", root_cid="R")
        )
    assert exc_info.value.code == "cloud115_source_inside_library"


def test_assert_cid_rejects_source_inside_root_subtree():
    from src.api.exception.errors import ApiError
    from src.service.playback.cloud115_backend_service import (
        assert_cid_outside_library_root,
    )

    # source S 的祖先链含 root R：0 -> R -> S
    client = _FakeDirInfoClient({"S": ("0", "R"), "R": ("0",)})
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            assert_cid_outside_library_root(client, source_cid="S", root_cid="R")
        )
    assert exc_info.value.code == "cloud115_source_inside_library"


def test_assert_cid_rejects_source_containing_root():
    from src.api.exception.errors import ApiError
    from src.service.playback.cloud115_backend_service import (
        assert_cid_outside_library_root,
    )

    # root R 的祖先链含 source S：0 -> S -> R（含 S="0" 选中整个根的情形）
    client = _FakeDirInfoClient({"R": ("0", "S"), "S": ("0",)})
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            assert_cid_outside_library_root(client, source_cid="S", root_cid="R")
        )
    assert exc_info.value.code == "cloud115_source_contains_library"


def test_assert_cid_rejects_root_directory_as_source():
    from src.api.exception.errors import ApiError
    from src.service.playback.cloud115_backend_service import (
        assert_cid_outside_library_root,
    )

    # source="0"（网盘根）：dir_info("0") 返回哨兵 paths=()，
    # 但 root R 的面包屑首环是根目录 "0" → 命中"源包含库"
    client = _FakeDirInfoClient({"R": ("0",)})
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            assert_cid_outside_library_root(client, source_cid="0", root_cid="R")
        )
    assert exc_info.value.code == "cloud115_source_contains_library"


def test_assert_cid_passes_for_sibling_directory():
    from src.service.playback.cloud115_backend_service import (
        assert_cid_outside_library_root,
    )

    # S 与 R 是根下平级目录，互不包含 → 放行
    client = _FakeDirInfoClient({"S": ("0",), "R": ("0",)})
    asyncio.run(
        assert_cid_outside_library_root(client, source_cid="S", root_cid="R")
    )
