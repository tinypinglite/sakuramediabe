from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.testclient import TestClient

from src.api.routers.deps import db_deps
from src.api.routers.playback import media as media_router
from src.common import build_signed_media_url


@pytest.fixture
def gateway(monkeypatch):
    # 真实路由与签名校验，替换数据读取和上游传输，不连接数据库或 115。
    state = SimpleNamespace(now=100.0)
    library = SimpleNamespace(
        id=3, provider_key="cloud115", provider_config={}, account_key="test"
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
    monkeypatch.setattr(
        media_router.MEDIA_PROVIDER_REGISTRY, "require", lambda _key: bundle
    )

    class Storage:
        async def handle_playback(self, *, media, context):
            if context.delivery == "redirect":
                return RedirectResponse("https://cdn.example/file.mp4", status_code=302)
            return Response(b"bytes", media_type="video/mp4", status_code=206)

    monkeypatch.setattr(
        media_router.MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage()
    )
    monkeypatch.setattr(
        media_router,
        "_AUTO_REDIRECT_RETRIES",
        media_router._AutoRedirectRetries(clock=lambda: state.now),
    )
    app = FastAPI()
    app.include_router(media_router.router)
    app.dependency_overrides[db_deps] = lambda: None
    with TestClient(app, follow_redirects=False) as client:

        def get(range_header=None, *, delivery="auto", user_agent="player"):
            headers = {"User-Agent": user_agent}
            if range_header is not None:
                headers["Range"] = range_header
            return client.get(
                build_signed_media_url(8088, delivery=delivery), headers=headers
            )

        state.get = get
        yield state


@pytest.mark.parametrize(
    "ranges",
    [
        ["bytes=0-", "bytes=290681502-", "bytes=44-", "bytes=26762-"],
        ["bytes=0-", "bytes=44-", "bytes=0-", "bytes=44-"],
        ["bytes=0-9", "bytes=0-19", "bytes=0-29", "bytes=0-39"],
        ["bytes=-10", "bytes=-20", "bytes=-30", "bytes=-40"],
        [None, "bytes=0-", None, "bytes=0-"],
        ["bytes=0-1,4-5", "bytes=0-1,6-7"] * 2,
    ],
)
def test_different_ranges_do_not_trigger_fallback(gateway, ranges):
    for value in ranges:
        assert gateway.get(value).status_code == 302
        gateway.now += 0.3


@pytest.mark.parametrize(
    "range_header", ["bytes=44-", "bytes=44-99", "bytes=-100", None]
)
def test_same_range_keeps_existing_retry_threshold_and_gap(gateway, range_header):
    for _ in range(3):
        assert gateway.get(range_header).status_code == 302
        gateway.now += 0.3
    assert gateway.get(range_header).status_code == 206
    gateway.now += 1.6
    assert gateway.get(range_header).status_code == 302


def test_range_change_resets_retry_count(gateway):
    for _ in range(3):
        assert gateway.get("bytes=44-").status_code == 302
    assert gateway.get("bytes=26762-").status_code == 302
    for _ in range(3):
        assert gateway.get("bytes=44-").status_code == 302
    assert gateway.get("bytes=44-").status_code == 206
    assert gateway.get("bytes=26762-").status_code == 302
