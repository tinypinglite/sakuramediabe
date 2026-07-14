import base64
from dataclasses import dataclass
from typing import Any

from src.lib.cloud115 import Cloud115CookieStatus, Cloud115RequestError, QrLoginResult
from src.lib.cloud115.types import DirEntry, QrCodeToken, QrStatus
from src.model import MediaLibrary
from src.service.playback import cloud115_qrlogin_service as qrlogin_module
from src.service.playback import media_library_service as media_library_service_module


COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"
NEW_COOKIES = "UID=12345678_A1_1800000000; CID=new; SEID=new"


@dataclass
class _FakeQrLogin:
    """按测试意图定制的 SDK 替身，覆盖 token/status/fetch_result 三条路径。"""

    token: QrCodeToken | None = None
    image: bytes = b""
    status: QrStatus = QrStatus.WAITING
    result: QrLoginResult | None = None
    fetch_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def get_token(self) -> QrCodeToken:
        assert self.token is not None
        return self.token

    async def get_qrcode_image(self, uid: str) -> bytes:
        return self.image

    async def get_qrcode_status(self, token: QrCodeToken) -> QrStatus:
        return self.status

    async def fetch_result(self, uid: str, app: str = "alipaymini") -> QrLoginResult:
        if self.fetch_error is not None:
            raise self.fetch_error
        assert self.result is not None
        return self.result


class _FakeClient:
    def __init__(self, cookies: str):
        self.cookies = cookies
        self.user_id = "12345678"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def probe_cookies_status(self) -> Cloud115CookieStatus:
        return Cloud115CookieStatus.ALIVE

    def snapshot_cookies(self) -> str:
        return self.cookies

    async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1000):
        # 根下已经有 sakuramedia，走 find 分支
        entries = [
            DirEntry(
                entry_id="cid-root",
                parent_id="0",
                name="sakuramedia",
                is_dir=True,
                size=0,
                sha1=None,
                pickcode="",
                mtime=0,
                ctime=0,
                is_video=False,
            )
        ]
        return entries, 1

    async def mkdir(self, pid: str, name: str) -> str:
        raise AssertionError("mkdir should not be called when root exists")


def _login(client, username="account", password="password123"):
    return client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    ).json()["access_token"]


def test_cloud115_endpoints_require_authentication(client):
    token_r = client.post("/media-libraries/cloud115/qrlogin/token")
    status_r = client.post(
        "/media-libraries/cloud115/qrlogin/status",
        json={"uid": "u", "time": 1, "sign": "s"},
    )
    create_r = client.post(
        "/media-libraries/cloud115",
        json={"name": "X", "uid": "u"},
    )
    reauth_r = client.post(
        "/media-libraries/cloud115/1/reauth",
        json={"uid": "u"},
    )

    assert token_r.status_code == 401
    assert status_r.status_code == 401
    assert create_r.status_code == 401
    assert reauth_r.status_code == 401


def test_get_qrlogin_token_returns_uid_and_base64_png(
    client, account_user, monkeypatch
):
    token_obj = QrCodeToken(uid="uid-1", time=1700000000, sign="sig", qrcode="raw")
    fake = _FakeQrLogin(token=token_obj, image=b"PNGDATA")
    monkeypatch.setattr(qrlogin_module, "Cloud115QrLogin", lambda: fake)

    access = _login(client, username=account_user.username)
    response = client.post(
        "/media-libraries/cloud115/qrlogin/token",
        headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == "uid-1"
    assert body["time"] == 1700000000
    assert body["sign"] == "sig"
    assert base64.b64decode(body["qrcode_png_base64"]) == b"PNGDATA"


def test_poll_qrlogin_status_forwards_confirmed(client, account_user, monkeypatch):
    fake = _FakeQrLogin(status=QrStatus.CONFIRMED)
    monkeypatch.setattr(qrlogin_module, "Cloud115QrLogin", lambda: fake)

    access = _login(client, username=account_user.username)
    response = client.post(
        "/media-libraries/cloud115/qrlogin/status",
        headers={"Authorization": f"Bearer {access}"},
        json={"uid": "uid-1", "time": 1, "sign": "s"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "confirmed"}


def test_create_cloud115_library_end_to_end(client, account_user, monkeypatch):
    """扫码 CONFIRMED 后调用创建端点：SDK 全走 fake，库落 backend=cloud115。"""
    fake_qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    monkeypatch.setattr(
        media_library_service_module, "Cloud115QrLogin", lambda: fake_qr
    )
    monkeypatch.setattr(
        media_library_service_module, "Cloud115Client", _FakeClient
    )

    access = _login(client, username=account_user.username)
    response = client.post(
        "/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "115主账号", "uid": "uid-abc"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "115主账号"
    assert body["backend"] == "cloud115"
    assert "cookies" not in body["backend_config"]
    assert body["backend_config"]["root_cid"] == "cid-root"
    assert body["backend_config"]["app"] == "alipaymini"
    # DB 也真落了
    library = MediaLibrary.get_by_id(body["id"])
    assert library.name == "115主账号"
    assert library.backend_config["cookies"] == COOKIES
    assert library.backend_account_key == "cloud115:12345678"


def test_media_library_list_never_exposes_cloud115_cookies(
    client, account_user
):
    _create_cloud_library_row(name="secret-cloud")
    access = _login(client, username=account_user.username)

    response = client.get(
        "/media-libraries",
        headers={"Authorization": f"Bearer {access}"},
    )

    assert response.status_code == 200
    serialized = response.text
    assert "cookies" not in serialized
    assert COOKIES not in serialized


def test_media_library_update_never_exposes_cloud115_cookies(
    client, account_user
):
    library = _create_cloud_library_row(name="rename-me")
    access = _login(client, username=account_user.username)

    response = client.patch(
        f"/media-libraries/{library.id}",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "renamed"},
    )

    assert response.status_code == 200
    assert response.json()["backend_config"] == {
        "root_cid": "cid-root",
        "app": "alipaymini",
    }
    assert COOKIES not in response.text


def test_create_cloud115_library_rejects_duplicate_account(
    client, account_user, monkeypatch
):
    existing = _create_cloud_library_row(name="existing")
    existing.backend_account_key = "cloud115:12345678"
    existing.save()
    fake_qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: fake_qr)
    monkeypatch.setattr(media_library_service_module, "Cloud115Client", _FakeClient)
    access = _login(client, username=account_user.username)

    response = client.post(
        "/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "duplicate", "uid": "uid-dup"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cloud115_account_already_bound"


def test_create_cloud115_library_maps_probe_unavailable_to_502(
    client, account_user, monkeypatch
):
    class _UnavailableClient(_FakeClient):
        async def probe_cookies_status(self) -> Cloud115CookieStatus:
            return Cloud115CookieStatus.UNAVAILABLE

    fake_qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=COOKIES, cookie_dict={}, user_id="12345678", app="alipaymini"
        )
    )
    monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: fake_qr)
    monkeypatch.setattr(media_library_service_module, "Cloud115Client", _UnavailableClient)
    access = _login(client, username=account_user.username)

    response = client.post(
        "/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "temporary", "uid": "uid-temp"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "cloud115_upstream_error"


def test_create_cloud115_library_reports_invalid_app(
    client, account_user, monkeypatch
):
    fake_qr = _FakeQrLogin()  # 不会被调
    monkeypatch.setattr(
        media_library_service_module, "Cloud115QrLogin", lambda: fake_qr
    )
    monkeypatch.setattr(media_library_service_module, "Cloud115Client", _FakeClient)

    access = _login(client, username=account_user.username)
    response = client.post(
        "/media-libraries/cloud115",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "X", "uid": "uid", "app": "martian"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cloud115_app"


def test_reauth_cloud115_library_updates_only_credentials(
    client, account_user, monkeypatch
):
    library = _create_cloud_library_row(name="reauth")
    original_root_cid = library.backend_config["root_cid"]
    fake_qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies=NEW_COOKIES, cookie_dict={}, user_id="12345678", app="web"
        )
    )
    monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: fake_qr)
    monkeypatch.setattr(media_library_service_module, "Cloud115Client", _FakeClient)
    access = _login(client, username=account_user.username)

    response = client.post(
        f"/media-libraries/cloud115/{library.id}/reauth",
        headers={"Authorization": f"Bearer {access}"},
        json={"uid": "uid-new", "app": "web"},
    )

    assert response.status_code == 200
    assert "cookies" not in response.text
    refreshed = MediaLibrary.get_by_id(library.id)
    assert refreshed.backend_config == {
        "cookies": NEW_COOKIES,
        "root_cid": original_root_cid,
        "app": "web",
    }
    assert refreshed.backend_account_key == "cloud115:12345678"


def test_reauth_cloud115_library_rejects_different_account(
    client, account_user, monkeypatch
):
    library = _create_cloud_library_row(name="reauth-mismatch")
    fake_qr = _FakeQrLogin(
        result=QrLoginResult(
            cookies="UID=999999_A1_1800000000; CID=x; SEID=y",
            cookie_dict={},
            user_id="999999",
            app="alipaymini",
        )
    )

    class _OtherAccountClient(_FakeClient):
        def __init__(self, cookies: str):
            super().__init__(cookies)
            self.user_id = "999999"

    monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: fake_qr)
    monkeypatch.setattr(media_library_service_module, "Cloud115Client", _OtherAccountClient)
    access = _login(client, username=account_user.username)

    response = client.post(
        f"/media-libraries/cloud115/{library.id}/reauth",
        headers={"Authorization": f"Bearer {access}"},
        json={"uid": "uid-other"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cloud115_account_mismatch"
    assert MediaLibrary.get_by_id(library.id).backend_config["cookies"] == COOKIES


def test_reauth_cloud115_library_maps_qr_upstream_error_to_502(
    client, account_user, monkeypatch
):
    library = _create_cloud_library_row(name="reauth-upstream")
    fake_qr = _FakeQrLogin(
        fetch_error=Cloud115RequestError(
            "timeout",
            method="POST",
            url="https://passportapi.115.com/login/qrcode/",
        )
    )
    monkeypatch.setattr(media_library_service_module, "Cloud115QrLogin", lambda: fake_qr)
    access = _login(client, username=account_user.username)

    response = client.post(
        f"/media-libraries/cloud115/{library.id}/reauth",
        headers={"Authorization": f"Bearer {access}"},
        json={"uid": "uid-upstream"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "cloud115_upstream_error"
    assert MediaLibrary.get_by_id(library.id).backend_config["cookies"] == COOKIES


class _FakeBrowseClient:
    """浏览端点的 SDK 替身：list_dir 返回固定条目；记录 snapshot 以验证 ctx 生命周期。"""

    def __init__(self, cookies: str):
        self._cookies = cookies
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def snapshot_cookies(self) -> str:
        return self._cookies

    async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1000):
        entries = [
            DirEntry(
                entry_id="dir-1", parent_id=cid, name="来源目录", is_dir=True,
                size=0, sha1=None, pickcode="", mtime=100, ctime=100, is_video=False,
            ),
            DirEntry(
                entry_id="file-1", parent_id=cid, name="ABP-123.mp4", is_dir=False,
                size=1024, sha1="AAA", pickcode="pc-1", mtime=200, ctime=200,
                is_video=True,
            ),
        ]
        return entries, 2


def _create_cloud_library_row(name="cloud"):
    return MediaLibrary.create(
        name=name,
        backend="cloud115",
        backend_account_key="cloud115:12345678",
        backend_config={"cookies": COOKIES, "root_cid": "cid-root", "app": "alipaymini"},
    )


def test_browse_cloud115_directory_returns_entries_and_root_cid(
    client, account_user, monkeypatch
):
    from src.service.playback import cloud115_backend_service as backend_module

    library = _create_cloud_library_row()
    monkeypatch.setattr(backend_module, "Cloud115Client", _FakeBrowseClient)

    access = _login(client, username=account_user.username)
    response = client.get(
        f"/media-libraries/cloud115/{library.id}/entries",
        headers={"Authorization": f"Bearer {access}"},
        params={"cid": "0", "limit": 50},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["root_cid"] == "cid-root"
    assert body["total"] == 2
    assert [e["entry_id"] for e in body["entries"]] == ["dir-1", "file-1"]
    assert body["entries"][0]["is_dir"] is True
    assert body["entries"][1]["is_video"] is True


def test_browse_cloud115_directory_rejects_local_library(client, account_user):
    library = MediaLibrary.create(
        name="local", backend="local", backend_config={"root_path": "/library/a"}
    )
    access = _login(client, username=account_user.username)
    response = client.get(
        f"/media-libraries/cloud115/{library.id}/entries",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_library_backend_mismatch"


def test_browse_cloud115_directory_maps_auth_error(client, account_user, monkeypatch):
    from src.lib.cloud115 import Cloud115AuthError
    from src.service.playback import cloud115_backend_service as backend_module

    class _DeadClient(_FakeBrowseClient):
        async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1000):
            raise Cloud115AuthError("cookies expired", errno=990009)

    library = _create_cloud_library_row(name="cloud-dead")
    monkeypatch.setattr(backend_module, "Cloud115Client", _DeadClient)

    access = _login(client, username=account_user.username)
    response = client.get(
        f"/media-libraries/cloud115/{library.id}/entries",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cloud115_cookies_invalid"
