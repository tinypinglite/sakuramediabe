from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.testclient import TestClient

from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers.deps import db_deps
from src.api.routers.playback import media as media_router
from src.common import build_signed_media_url
from src.plugins.provider_protocol import ProviderOperationError


@pytest.fixture
def gateway(monkeypatch):
    # 真实路由与签名校验，替换数据读取和上游传输，不连接数据库或 115。
    state = SimpleNamespace(contexts=[], error=None)
    library = SimpleNamespace(
        id=3, provider_key="example-provider", provider_config={}, account_key="test"
    )
    media = SimpleNamespace(
        id=8088,
        library=library,
        storage_ref={},
        file_name="001.mp4",
        file_size_bytes=290884572,
        duration_seconds=589,
    )
    monkeypatch.setattr(media_router.Media, "get_or_none", lambda *_args: media)
    bundle = SimpleNamespace(playback_deliveries=("redirect", "proxy"))
    state.bundle = bundle
    monkeypatch.setattr(
        media_router.MEDIA_PROVIDER_REGISTRY, "require", lambda _key: bundle
    )

    class Storage:
        async def handle_playback(self, *, media, context):
            state.contexts.append(context)
            if state.error is not None:
                raise state.error
            if context.delivery == "redirect":
                return RedirectResponse("https://cdn.example/file.mp4", status_code=302)
            return Response(b"bytes", media_type="video/mp4", status_code=206)

    monkeypatch.setattr(
        media_router.MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage()
    )
    app = FastAPI()
    app.include_router(media_router.router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[db_deps] = lambda: None
    with TestClient(app, follow_redirects=False) as client:

        def get(range_header=None, *, delivery=None, user_agent="player"):
            headers = {"User-Agent": user_agent}
            if range_header is not None:
                headers["Range"] = range_header
            url = build_signed_media_url(8088, delivery=delivery or "proxy")
            if delivery is None:
                url = url.replace("&delivery=proxy", "")
            return client.get(url, headers=headers)

        state.get = get
        yield state


@pytest.mark.parametrize(
    "deliveries, expected_status",
    [(("proxy", "redirect"), 206), (("redirect", "proxy"), 302), (("proxy",), 206)],
)
def test_missing_delivery_uses_provider_declaration(
    gateway, deliveries, expected_status
):
    gateway.bundle.playback_deliveries = deliveries
    assert gateway.get().status_code == expected_status


@pytest.mark.parametrize("delivery, status", [("proxy", 206), ("redirect", 302)])
def test_explicit_delivery_is_preserved(gateway, delivery, status):
    gateway.bundle.playback_deliveries = ("proxy", "redirect")
    for value in [None, "bytes=0-", "bytes=44-", "bytes=26762-"] * 2:
        assert gateway.get(value, delivery=delivery).status_code == status
    assert all(c.delivery == delivery for c in gateway.contexts)
    assert (
        gateway.contexts[-1].url_for("hls/segment.ts").endswith(f"delivery={delivery}")
    )


@pytest.mark.parametrize(
    "code,retryable,status",
    [
        ("unsupported", False, 422),
        ("unavailable", True, 503),
        ("source_not_found", False, 404),
    ],
)
def test_provider_error_does_not_change_delivery(gateway, code, retryable, status):
    gateway.error = ProviderOperationError(
        "example-provider", "playback", code, "test error", retryable
    )
    assert gateway.get(delivery="redirect").status_code == status
    assert [c.delivery for c in gateway.contexts] == ["redirect"]


def test_rejects_auto(gateway):
    # 直接构造旧请求，URL helper 已不允许生成 auto。
    app = FastAPI()
    app.include_router(media_router.router)
    app.dependency_overrides[db_deps] = lambda: None
    with TestClient(app) as client:
        url = build_signed_media_url(8088).replace("delivery=proxy", "delivery=auto")
        assert client.get(url).status_code == 422
    assert gateway.contexts == []


def test_rejects_unsupported_delivery(gateway):
    gateway.bundle.playback_deliveries = ("proxy",)
    assert gateway.get(delivery="redirect").status_code == 422
    assert gateway.contexts == []
