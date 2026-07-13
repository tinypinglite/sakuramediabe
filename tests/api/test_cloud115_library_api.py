import base64
from dataclasses import dataclass
from typing import Any

from src.lib.cloud115 import QrLoginResult
from src.lib.cloud115.types import DirEntry, QrCodeToken, QrStatus
from src.model import MediaLibrary
from src.service.playback import cloud115_qrlogin_service as qrlogin_module
from src.service.playback import media_library_service as media_library_service_module


COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"


@dataclass
class _FakeQrLogin:
    """按测试意图定制的 SDK 替身，覆盖 token/status/fetch_result 三条路径。"""

    token: QrCodeToken | None = None
    image: bytes = b""
    status: QrStatus = QrStatus.WAITING
    result: QrLoginResult | None = None

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
        assert self.result is not None
        return self.result


class _FakeClient:
    def __init__(self, cookies: str):
        self.cookies = cookies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def check_cookies_alive(self) -> bool:
        return True

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

    assert token_r.status_code == 401
    assert status_r.status_code == 401
    assert create_r.status_code == 401


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
    assert body["backend_config"]["cookies"] == COOKIES
    assert body["backend_config"]["root_cid"] == "cid-root"
    assert body["backend_config"]["app"] == "alipaymini"
    # DB 也真落了
    assert MediaLibrary.get_by_id(body["id"]).name == "115主账号"


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
