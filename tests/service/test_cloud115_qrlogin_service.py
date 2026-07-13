from dataclasses import dataclass
from typing import Any

import pytest

from src.api.exception.errors import ApiError
from src.lib.cloud115.types import QrCodeToken, QrStatus
from src.schema.playback.cloud115_libraries import Cloud115QrStatusRequest
from src.service.playback import Cloud115QrLoginService
from src.service.playback import cloud115_qrlogin_service as qrlogin_module


@dataclass
class _FakeQrLogin:
    token: QrCodeToken | None = None
    image: bytes = b""
    status: QrStatus = QrStatus.WAITING
    status_calls: list[QrCodeToken] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        pass

    async def get_token(self) -> QrCodeToken:
        assert self.token is not None
        return self.token

    async def get_qrcode_image(self, uid: str) -> bytes:
        assert self.token is not None
        assert uid == self.token.uid
        return self.image

    async def get_qrcode_status(self, token: QrCodeToken) -> QrStatus:
        if self.status_calls is not None:
            self.status_calls.append(token)
        return self.status


async def test_get_token_returns_uid_and_base64_png(monkeypatch):
    qr = _FakeQrLogin(
        token=QrCodeToken(uid="uid-1", time=1700000000, sign="sign-1", qrcode="raw"),
        image=b"\x89PNG-mock-bytes",
    )
    monkeypatch.setattr(qrlogin_module, "Cloud115QrLogin", lambda: qr)

    resource = await Cloud115QrLoginService.get_token()

    assert resource.uid == "uid-1"
    assert resource.time == 1700000000
    assert resource.sign == "sign-1"
    # base64 编码后 decode 回来 = 原字节
    import base64
    assert base64.b64decode(resource.qrcode_png_base64) == b"\x89PNG-mock-bytes"


async def test_poll_status_maps_qrstatus_to_string(monkeypatch):
    """SDK 的 QrStatus 枚举 → 前端字符串（各个值都要映射到）。"""
    for sdk_status, expected in [
        (QrStatus.WAITING, "waiting"),
        (QrStatus.SCANNED, "scanned"),
        (QrStatus.CONFIRMED, "confirmed"),
        (QrStatus.EXPIRED, "expired"),
        (QrStatus.CANCELED, "canceled"),
    ]:
        qr = _FakeQrLogin(status=sdk_status)
        monkeypatch.setattr(qrlogin_module, "Cloud115QrLogin", lambda qr=qr: qr)

        resource = await Cloud115QrLoginService.poll_status(
            Cloud115QrStatusRequest(uid="u", time=1, sign="s")
        )
        assert resource.status == expected


async def test_poll_status_forwards_uid_time_sign(monkeypatch):
    """service 层要把请求里的 uid/time/sign 原样构造成 QrCodeToken 传给 SDK。"""
    seen: list[QrCodeToken] = []
    qr = _FakeQrLogin(status=QrStatus.WAITING, status_calls=seen)
    monkeypatch.setattr(qrlogin_module, "Cloud115QrLogin", lambda: qr)

    await Cloud115QrLoginService.poll_status(
        Cloud115QrStatusRequest(uid="uid-x", time=42, sign="sig-x")
    )
    assert len(seen) == 1
    tok = seen[0]
    assert tok.uid == "uid-x"
    assert tok.time == 42
    assert tok.sign == "sig-x"


def test_validate_app_accepts_known():
    assert Cloud115QrLoginService.validate_app("alipaymini") == "alipaymini"
    assert Cloud115QrLoginService.validate_app("web") == "web"


def test_validate_app_rejects_unknown():
    with pytest.raises(ApiError) as exc_info:
        Cloud115QrLoginService.validate_app("mars")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_cloud115_app"
