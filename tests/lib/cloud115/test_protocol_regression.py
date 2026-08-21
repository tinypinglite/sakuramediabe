"""Cloud115 HLS 协议解析回归测试，全部使用本地 HTTP mock。"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.lib.cloud115 import (
    Cloud115Client,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115VideoNotReadyError,
)

_MOCK_COOKIE = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz; KID=kkk"
_MASTER_M3U8_URL = "https://115.com/api/video/m3u8/cd5abc.m3u8"
_HD_M3U8_URL = "https://cpats01.115.com/xyz/HASH_1280.m3u8?u=1&s=4194304"
_MASTER_M3U8 = (
    "#EXTM3U\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1800000,RESOLUTION=1280x720,NAME="HD"\r\n'
    f"{_HD_M3U8_URL}\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=640x360,NAME="SD"\r\n'
    "https://cpats01.115.com/xyz/HASH_640.m3u8?u=1&s=4194304\r\n"
)
_VARIANT_M3U8 = (
    "#EXTM3U\r\n"
    "#EXTINF:10.000000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00001.ts?u=1&s=4194304\r\n"
    "#EXTINF:9.500000,\r\n"
    "/xyz/HASH_1280-00002.ts?u=1&s=4194304\r\n"
    "#EXTINF:5.123000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00003.ts?u=1&s=4194304\r\n"
    "#EXT-X-ENDLIST\r\n"
)
_VIDEO_INFO_PAYLOAD = {
    "state": True,
    "video_url": _MASTER_M3U8_URL,
    "thumb_url": "https://static.115.com/video/HASH.jpg",
    "width": "1280",
    "height": "720",
    "file_status": 1,
}


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    user_agent: str | None = None,
) -> Cloud115Client:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Cloud115Client(
        cookies=_MOCK_COOKIE,
        user_agent=user_agent,
        http_client=http_client,
    )


async def test_dir_info_root_sentinel_does_not_request_remote() -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"root dir lookup should not request 115: {request.url}")

    client = _mock_client(unexpected_request)
    try:
        directory = await client.dir_info("0")
        assert directory.cid == "0"
        assert directory.name == "根目录"
        assert directory.pickcode == ""
        assert directory.paths == ()
    finally:
        await client._client.aclose()


async def test_get_video_info_parses_historical_master_playlist() -> None:
    player_user_agent = "SakuraMedia-HLS-Test/1.0"

    def handler(request: httpx.Request) -> httpx.Response:
        # files/video 和后续 m3u8 必须使用同一播放器 UA，115 会把它写入 URL 签名。
        assert request.headers["User-Agent"] == player_user_agent
        if request.url.host == "webapi.115.com":
            assert request.url.path == "/files/video"
            assert request.url.params["pickcode"] == "cd5abc"
            return httpx.Response(200, json=_VIDEO_INFO_PAYLOAD)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_MASTER_M3U8.encode())
        raise AssertionError(f"unexpected request: {request.url}")

    client = _mock_client(handler, user_agent=player_user_agent)
    try:
        info = await client.get_video_info("cd5abc")
        assert info.width == 1280
        assert info.height == 720
        assert [definition.bandwidth for definition in info.definitions] == [
            1800000,
            800000,
        ]
        assert info.definitions[0].label == "HD"
        assert info.definitions[0].m3u8_url == _HD_M3U8_URL
    finally:
        await client._client.aclose()


@pytest.mark.parametrize("file_status", [0, 2])
async def test_get_video_info_rejects_video_not_ready(file_status: int) -> None:
    client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": True, "file_status": file_status, "video_url": ""},
        )
    )
    try:
        with pytest.raises(Cloud115VideoNotReadyError) as error:
            await client.get_video_info("cd5abc")
        assert error.value.file_status == file_status
    finally:
        await client._client.aclose()


async def test_get_video_info_maps_membership_and_non_video_errors() -> None:
    membership_client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": False, "errno": 406, "error": "需要VIP会员"},
        )
    )
    non_video_client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": True, "video_url": "", "width": 0, "height": 0},
        )
    )
    try:
        with pytest.raises(Cloud115MembershipRequiredError):
            await membership_client.get_video_info("cd5abc")
        with pytest.raises(Cloud115NotFoundError, match="video_url missing"):
            await non_video_client.get_video_info("cd5abc")
    finally:
        await membership_client._client.aclose()
        await non_video_client._client.aclose()


async def test_get_video_segments_selects_highest_and_parses_relative_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com":
            return httpx.Response(200, json=_VIDEO_INFO_PAYLOAD)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_MASTER_M3U8.encode())
        if str(request.url) == _HD_M3U8_URL:
            return httpx.Response(200, content=_VARIANT_M3U8.encode())
        raise AssertionError(f"unexpected request: {request.url}")

    client = _mock_client(handler)
    try:
        segments = await client.get_video_segments("cd5abc")
        assert [segment.duration_seconds for segment in segments] == [10.0, 9.5, 5.123]
        assert segments[1].url == (
            "https://cpats01.115.com/xyz/HASH_1280-00002.ts?u=1&s=4194304"
        )
    finally:
        await client._client.aclose()


def test_m3u8_parsers_handle_missing_attributes_and_empty_input() -> None:
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:RESOLUTION=1280x720\n"
        "relative.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=500000\n"
        "https://example.com/variant.m3u8\n"
    )
    definitions = Cloud115Client._parse_master_m3u8(
        master,
        base_url="https://example.com/master/index.m3u8",
    )
    assert definitions[0].bandwidth == 0
    assert definitions[0].m3u8_url == "https://example.com/master/relative.m3u8"
    assert definitions[1].resolution == ""
    assert definitions[1].label == ""
    assert Cloud115Client._parse_master_m3u8("", base_url="https://example.com") == []
    assert Cloud115Client._parse_variant_m3u8("", base_url="https://example.com") == []


def test_pick_variant_prefers_exact_bandwidth_then_highest() -> None:
    definitions = Cloud115Client._parse_master_m3u8(
        _MASTER_M3U8,
        base_url=_MASTER_M3U8_URL,
    )
    assert Cloud115Client._pick_variant(definitions, 800000).bandwidth == 800000
    assert Cloud115Client._pick_variant(definitions, 9999999).bandwidth == 1800000
