"""Cloud115QrLogin 的 transport / HTTP / JSON 异常边界。"""

import httpx
import pytest

from src.lib.cloud115 import (
    Cloud115QrLogin,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    QrCodeToken,
    QrStatus,
)


async def test_fetch_result_wraps_transport_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RequestError) as exc_info:
            await qr.fetch_result("uid-1")

    assert exc_info.value.method == "POST"
    assert exc_info.value.url is not None


async def test_fetch_result_wraps_non_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RequestError, match="invalid JSON"):
            await qr.fetch_result("uid-1")


async def test_status_http_error_is_not_reported_as_waiting():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable", request=request)

    token = QrCodeToken(uid="uid-1", time=1, sign="s", qrcode="")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RequestError) as exc_info:
            await qr.get_qrcode_status(token)

    assert "http 503" in str(exc_info.value)


async def test_status_long_poll_timeout_remains_waiting():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("long poll timeout", request=request)

    token = QrCodeToken(uid="uid-1", time=1, sign="s", qrcode="")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        assert await qr.get_qrcode_status(token) is QrStatus.WAITING


async def test_status_connect_timeout_is_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout", request=request)

    token = QrCodeToken(uid="uid-1", time=1, sign="s", qrcode="")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RequestError, match="connect timeout"):
            await qr.get_qrcode_status(token)


async def test_token_invalid_fields_are_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"uid": "uid-1", "time": "bad", "sign": "s"}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RequestError, match="invalid fields"):
            await qr.get_token()


async def test_qrcode_rate_limit_preserves_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "12"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        qr = Cloud115QrLogin(http_client=http_client)
        with pytest.raises(Cloud115RateLimitedError) as exc_info:
            await qr.get_token()

    assert exc_info.value.retry_after_seconds == 12
