"""115 HLS 全量代理路由（/media/*.m3u8 + /media/hls-segment）API 测试。

签名与路由行为在 TestClient 内验证；布局构建与分段转发打桩，不触达真实 115。
"""

import asyncio
import hmac
import hashlib

import pytest
from fastapi.responses import StreamingResponse

from src.service.playback.cloud115_hls_proxy_service import (
    Cloud115HlsProxyService,
    HlsSegment,
    MergedHlsLayout,
)

from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


def _sig(media_id: int) -> str:
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        f"media:{media_id}:{TEST_FILE_SIGNATURE_EXPIRES}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _layout(media_ids: list[int], seg_counts: list[int]) -> MergedHlsLayout:
    segments: list[HlsSegment] = []
    for mid, count in zip(media_ids, seg_counts):
        segments.extend(
            HlsSegment(mid, i, 10.0, f"https://cdn.example/{mid}/{i}.ts")
            for i in range(count)
        )
    discontinuity = frozenset({seg_counts[0]}) if len(media_ids) > 1 else frozenset()
    return MergedHlsLayout(
        media_ids=tuple(media_ids),
        segments=tuple(segments),
        discontinuity_indexes=discontinuity,
        total_duration=float(10 * len(segments)),
        target_duration=11,
    )


def _proxy_layout(media_ids, seg_counts):
    async def _build(ids, ua):
        return _layout(media_ids, seg_counts)

    return _build


@pytest.fixture(autouse=True)
def _clear_proxy_caches():
    Cloud115HlsProxyService._layout_cache.clear()
    yield
    Cloud115HlsProxyService._layout_cache.clear()


class TestM3u8Playlist:
    def test_single_stream_m3u8(self, client, monkeypatch):
        monkeypatch.setattr(
            Cloud115HlsProxyService,
            "build_merged_layout",
            _proxy_layout([1], [2]),
        )
        url = f"/media/1/stream.m3u8?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(1)}"
        response = client.get(url, headers={"User-Agent": "ExoPlayer/1"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        text = response.text
        assert text.startswith("#EXTM3U")
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
        assert "#EXT-X-DISCONTINUITY" not in text
        assert (
            f"/media/hls-segment/0.ts?media_ids=1&expires={TEST_FILE_SIGNATURE_EXPIRES}"
            f"&signature={_sig(1)}" in text
        )

    def test_merged_stream_m3u8(self, client, monkeypatch):
        monkeypatch.setattr(
            Cloud115HlsProxyService,
            "build_merged_layout",
            _proxy_layout([1, 2], [2, 2]),
        )
        url = (
            f"/media/merged-stream.m3u8?media_ids=1,2"
            f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(1)}"
        )
        response = client.get(url, headers={"User-Agent": "ExoPlayer/1"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.apple.mpegurl"
        text = response.text
        assert text.count("#EXT-X-DISCONTINUITY") == 1
        assert (
            f"/media/hls-segment/2.ts?media_ids=1,2&expires={TEST_FILE_SIGNATURE_EXPIRES}"
            f"&signature={_sig(1)}" in text
        )

    def test_bad_signature_rejected(self, client):
        response = client.get(
            f"/media/1/stream.m3u8?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature=deadbeef"
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "file_signature_invalid"

    def test_missing_signed_params_rejected(self, client):
        response = client.get("/media/1/stream.m3u8")
        assert response.status_code == 403

    def test_merged_missing_media_ids_rejected(self, client):
        response = client.get(
            f"/media/merged-stream.m3u8?expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(1)}"
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "merged_hls_need_at_least_one"

    def test_merged_signature_anchored_on_first_media(self, client, monkeypatch):
        # media_ids=[5,6] 时签名应锚定 5（与本地合并同机制），6 的签名应被拒。
        monkeypatch.setattr(
            Cloud115HlsProxyService,
            "build_merged_layout",
            _proxy_layout([5, 6], [1, 1]),
        )
        url = (
            f"/media/merged-stream.m3u8?media_ids=5,6"
            f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(5)}"
        )
        assert client.get(url).status_code == 200
        bad = (
            f"/media/merged-stream.m3u8?media_ids=5,6"
            f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(6)}"
        )
        assert client.get(bad).status_code == 403


class TestHlsSegment:
    def test_proxies_segment_bytes(self, client, monkeypatch):
        monkeypatch.setattr(
            Cloud115HlsProxyService,
            "build_merged_layout",
            _proxy_layout([1, 2], [2, 2]),
        )

        async def _proxy(url, ua):
            return StreamingResponse(
                iter([b"ts-bytes"]),
                media_type="video/mp2t",
                headers={"Cache-Control": "no-store"},
            )

        monkeypatch.setattr(Cloud115HlsProxyService, "proxy_segment", _proxy)
        url = (
            f"/media/hls-segment/3.ts?media_ids=1,2"
            f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(1)}"
        )
        response = client.get(url, headers={"User-Agent": "ExoPlayer/1"})
        assert response.status_code == 200
        assert response.content == b"ts-bytes"
        assert response.headers["content-type"] == "video/mp2t"

    def test_segment_out_of_range(self, client, monkeypatch):
        monkeypatch.setattr(
            Cloud115HlsProxyService,
            "build_merged_layout",
            _proxy_layout([1], [2]),
        )
        url = (
            f"/media/hls-segment/5.ts?media_ids=1"
            f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={_sig(1)}"
        )
        response = client.get(url)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "hls_segment_not_found"
