"""MediaService 测试：cloud115 直链缓存（命中/未命中/失效）。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from src.lib.cloud115.types import DirectUrl
from src.model import Image, Media, MediaLibrary, VideoItem
from src.service.playback.media_service import MediaService


@pytest.fixture()
def media_service_tables(test_db):
    # Media 挂 VideoItem 归属；VideoItem.cover_image 是 nullable FK 但表必须存在。
    models = [Image, MediaLibrary, VideoItem, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    # 每个用例都用独立进程内缓存，避免 xdist 顺序污染。
    MediaService._cloud115_url_cache.clear()
    yield test_db
    MediaService._cloud115_url_cache.clear()


def _make_cloud115_media(*, pickcode: str = "pc-abc") -> Media:
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={"cookies": "UID=1_A_1;", "root_cid": "3", "app": "alipaymini"},
    )
    video = VideoItem.create(title="fixture-video")
    return Media.create(
        library=library,
        video_item=video,
        backend_locator={"pickcode": pickcode, "fid": "fid-1"},
        file_size_bytes=1024,
    )


class _StubClient:
    """替 Cloud115Client：只实现测试用到的 get_download_url，记录调用次数。"""

    def __init__(self, url: str = "https://cdn.115.com/x?t=999&f=abc"):
        self._url = url
        self.calls: list[tuple[str, str]] = []

    async def get_download_url(self, pickcode: str, user_agent: str) -> DirectUrl:
        self.calls.append((pickcode, user_agent))
        return DirectUrl(
            file_id="fid-1",
            file_name="a.mp4",
            file_size=1024,
            sha1="sha",
            pickcode=pickcode,
            url=self._url,
            user_agent=user_agent,
            expires_at=999,
        )


def _patch_client(monkeypatch, stub: _StubClient) -> None:
    @asynccontextmanager
    async def _fake_client_for(_library):
        yield stub

    monkeypatch.setattr(
        "src.service.playback.cloud115_backend_service.cloud115_client_for",
        _fake_client_for,
    )


def test_resolve_cloud115_stream_url_first_call_hits_upstream(
    media_service_tables, monkeypatch
):
    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    url = asyncio.run(
        MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a")
    )

    assert url == stub._url
    assert stub.calls == [("pc-abc", "UA/1.0")]


def test_resolve_cloud115_stream_url_second_call_hits_cache(
    media_service_tables, monkeypatch
):
    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    first = asyncio.run(
        MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a")
    )
    second = asyncio.run(
        MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a")
    )

    assert first == second
    # 命中缓存后不应二次访问 SDK。
    assert stub.calls == [("pc-abc", "UA/1.0")]


def test_resolve_cloud115_stream_url_different_user_agent_misses(
    media_service_tables, monkeypatch
):
    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a"))
    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/2.0", "sig-a"))

    # UA 不同触发重取——115 f= 指纹绑 UA，共享会 403。
    assert [call[1] for call in stub.calls] == ["UA/1.0", "UA/2.0"]


def test_resolve_cloud115_stream_url_different_signature_misses(
    media_service_tables, monkeypatch
):
    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a"))
    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-b"))

    assert len(stub.calls) == 2


def test_resolve_cloud115_stream_url_expired_entry_refreshes(
    media_service_tables, monkeypatch
):
    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a"))
    # 手动把该条目改成已过期，模拟 TTL 到期。
    key = (media.id, "sig-a", "UA/1.0")
    url, _ = MediaService._cloud115_url_cache[key]
    MediaService._cloud115_url_cache[key] = (url, 0.0)

    asyncio.run(MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a"))

    # 过期项应触发重取，并写回新的过期时间。
    assert len(stub.calls) == 2
    _, fresh_expires = MediaService._cloud115_url_cache[key]
    assert fresh_expires > 0.0


def test_resolve_cloud115_stream_url_cache_hit_logs_info(
    media_service_tables, monkeypatch, caplog
):
    import logging

    media = _make_cloud115_media()
    stub = _StubClient()
    _patch_client(monkeypatch, stub)

    # loguru 需要显式桥接到 caplog；只在这个用例开一个 sink。
    from loguru import logger

    sink_id = logger.add(caplog.handler, level="INFO", format="{message}")
    try:
        with caplog.at_level(logging.INFO):
            asyncio.run(
                MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a")
            )
            asyncio.run(
                MediaService.resolve_cloud115_stream_url(media, "UA/1.0", "sig-a")
            )
    finally:
        logger.remove(sink_id)

    assert any("cloud115 stream url cache hit" in rec.message for rec in caplog.records)
