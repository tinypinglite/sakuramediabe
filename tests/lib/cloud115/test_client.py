"""Cloud115Client 单元测试 —— 用 httpx.MockTransport 断言外发请求与响应解析。

覆盖：5 个接口的 URL/params/body 构造 + 响应字段映射 + errno 映射 + 429/5xx 处理。
所有 async 用例走 pytest-asyncio auto 模式（pyproject asyncio_mode = "auto"）。
"""

from __future__ import annotations

import base64
import json
from typing import Callable

import httpx
import pytest

import src.lib.cloud115.client as client_module
from src.lib.cloud115.client import Cloud115Client
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115OfflineQuotaExceededError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    Cloud115VideoNotReadyError,
)


COOKIE = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz; KID=kkk"
USER_ID = "12345678"


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cookies: str = COOKIE,
) -> Cloud115Client:
    """构造一个用 MockTransport 的客户端。"""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    return Cloud115Client(cookies=cookies, http_client=http_client)


# ---------------------------------------------------------------------------
# 构造函数 / user_id 解析
# ---------------------------------------------------------------------------


def test_ctor_parses_user_id_from_uid() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True}))
    assert client._user_id == USER_ID


def test_ctor_rejects_cookies_without_uid() -> None:
    with pytest.raises(Cloud115AuthError, match="UID"):
        Cloud115Client(cookies="CID=abc; SEID=xyz")


def test_ctor_rejects_empty_cookies() -> None:
    with pytest.raises(Cloud115AuthError):
        Cloud115Client(cookies="")


def test_ctor_rejects_malformed_uid() -> None:
    # UID 存在但格式不对（缺后缀）
    with pytest.raises(Cloud115AuthError, match="UID"):
        Cloud115Client(cookies="UID=notanumber; CID=abc")


# ---------------------------------------------------------------------------
# check_cookies_alive
# ---------------------------------------------------------------------------


async def test_check_cookies_alive_returns_true_on_state_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "my.115.com"
        assert request.url.params["ct"] == "guide"
        assert request.url.params["ac"] == "status"
        assert request.headers["Cookie"] == COOKIE
        return httpx.Response(200, json={"state": True, "data": {}})

    client = _make_client(handler)
    assert await client.check_cookies_alive() is True
    await client.close()


async def test_check_cookies_alive_returns_false_on_state_false() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": False, "error": "expired"}))
    assert await client.check_cookies_alive() is False
    await client.close()


async def test_check_cookies_alive_returns_false_on_302_redirect() -> None:
    """cookies 死了通常 302 到登录页；我们靠 follow_redirects=False 捕获这个信号。"""
    client = _make_client(lambda r: httpx.Response(302, headers={"Location": "https://passport.115.com/login"}))
    assert await client.check_cookies_alive() is False
    await client.close()


async def test_check_cookies_alive_returns_false_on_non_json_body() -> None:
    client = _make_client(lambda r: httpx.Response(200, content=b"<html>not json</html>"))
    assert await client.check_cookies_alive() is False
    await client.close()


async def test_check_cookies_alive_returns_false_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mock network down")

    client = _make_client(handler)
    # 契约：check_cookies_alive 永不抛，网络错也返 False
    assert await client.check_cookies_alive() is False
    await client.close()


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


async def test_list_dir_builds_request_and_parses_entries() -> None:
    sample = {
        "state": True,
        "count": 2,
        "offset": 0,
        "limit": 50,
        "data": [
            {"cid": "999", "pid": "0", "n": "some folder", "pc": "cd5folder"},   # 目录
            {"fid": "111", "cid": "0", "n": "video.mp4", "s": "123456",
             "sha": "SHA1ABC", "pc": "cd5file", "te": 1700000000, "tp": 1600000000, "iv": 1},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "webapi.115.com"
        assert request.url.path == "/files"
        assert request.url.params["cid"] == "0"
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "50"
        assert request.url.params["aid"] == "1"
        assert request.url.params["show_dir"] == "1"
        return httpx.Response(200, json=sample)

    client = _make_client(handler)
    entries, total = await client.list_dir("0", limit=50)
    assert total == 2
    assert len(entries) == 2
    # 目录
    assert entries[0].is_dir is True
    assert entries[0].entry_id == "999"
    assert entries[0].parent_id == "0"
    assert entries[0].name == "some folder"
    assert entries[0].pickcode == "cd5folder"
    assert entries[0].size == 0
    assert entries[0].sha1 is None
    # 文件
    assert entries[1].is_dir is False
    assert entries[1].entry_id == "111"
    assert entries[1].parent_id == "0"
    assert entries[1].name == "video.mp4"
    assert entries[1].size == 123456
    assert entries[1].sha1 == "SHA1ABC"
    assert entries[1].pickcode == "cd5file"
    assert entries[1].mtime == 1700000000
    assert entries[1].ctime == 1600000000
    assert entries[1].is_video is True
    await client.close()


async def test_list_dir_raises_value_error_on_limit_overflow() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "count": 0, "data": []}))
    with pytest.raises(ValueError, match="exceeds server max"):
        await client.list_dir("0", limit=2000)
    await client.close()


async def test_list_dir_state_false_maps_to_auth_error() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 990009, "error": "not logged in"
    }))
    with pytest.raises(Cloud115AuthError):
        await client.list_dir("0")
    await client.close()


async def test_errno_99_maps_to_auth_error() -> None:
    """errno=99 "请重新登录"：短时高频调用 downurl 触发的账号级冷却。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 99, "error": "请重新登录"
    }))
    with pytest.raises(Cloud115AuthError):
        await client.list_dir("0")
    await client.close()


async def test_errno_406_maps_to_membership_required_error() -> None:
    """errno=406 "需要VIP会员"：视频在线预览/m3u8 转码等 VIP 专属接口的策略拒绝。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 406, "error": "需要VIP会员"
    }))
    with pytest.raises(Cloud115MembershipRequiredError):
        await client.list_dir("0")
    await client.close()


async def test_list_dir_state_false_maps_to_not_found_for_990002() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 990002, "error": "parent dir gone"
    }))
    with pytest.raises(Cloud115NotFoundError):
        await client.list_dir("nonexistent-cid")
    await client.close()


async def test_list_dir_state_false_unknown_errno_falls_back_to_base() -> None:
    """未识别 errno 落到 Cloud115Error，不静默吞。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 42, "error": "who knows"
    }))
    with pytest.raises(Cloud115Error) as info:
        await client.list_dir("0")
    # 精确类型不是 AuthError/NotFoundError 等子类
    assert type(info.value) is Cloud115Error
    await client.close()


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------


async def test_file_info_builds_request_and_parses_entry() -> None:
    sample = {
        "state": True,
        "data": [{
            "fid": "111",
            "cid": "0",
            "n": "video.mp4",
            "s": "999",
            "sha": "SHAX",
            "pc": "cd5xxx",
            "te": 1710000000,
            "tp": 1610000000,
            "iv": 1,
        }],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "webapi.115.com"
        assert request.url.path == "/files/get_info"
        assert request.url.params["file_id"] == "111"
        return httpx.Response(200, json=sample)

    client = _make_client(handler)
    meta = await client.file_info("111")
    assert meta.file_id == "111"
    assert meta.name == "video.mp4"
    assert meta.size == 999
    assert meta.sha1 == "SHAX"
    assert meta.pickcode == "cd5xxx"
    assert meta.is_video is True
    await client.close()


async def test_file_info_empty_data_raises_not_found() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "data": []}))
    with pytest.raises(Cloud115NotFoundError, match="not found"):
        await client.file_info("bogus-id")
    await client.close()


# ---------------------------------------------------------------------------
# get_download_url
# ---------------------------------------------------------------------------


async def test_get_download_url_encrypts_payload_and_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心断言：外发请求结构 + 响应字段映射。

    p115 的 rsa_encode / rsa_decode 不是逆变换（都用 pow(x, e, n)），
    所以本测试通过 monkeypatch 掉 decrypt_response 直接注入解密结果，
    绕开"用 encode 造密文再让 decode 解开"这条不成立的路径。
    真实协议兼容性靠 integration test 打真实端点验证。
    """
    file_id = "9999"
    direct_url = "https://cdnfhnkc.115.com/xxx.mkv?t=1800000000&f=1"
    fake_decrypted = {
        file_id: {
            "file_name": "movie.mkv",
            "file_size": "1073741824",
            "pick_code": "cd5abc",
            "sha1": "SHA1XXX",
            "url": {"url": direct_url},
        }
    }
    monkeypatch.setattr(client_module, "decrypt_response", lambda _b64: fake_decrypted)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "proapi.115.com"
        assert request.url.path == "/app/chrome/downurl"
        assert request.method == "POST"
        assert request.headers["Referer"] == "https://115.com/"
        assert request.headers["User-Agent"] == "TestPlayerUA/1.0"
        # form body 里 data=<base64>
        body_str = request.content.decode("ascii")
        assert body_str.startswith("data=")
        from urllib.parse import parse_qs
        parsed = parse_qs(body_str)
        cipher_b64 = parsed["data"][0]
        # 断言 encode 的 base64 输出确实是 128 字节的倍数
        raw = base64.b64decode(cipher_b64)
        assert len(raw) % 128 == 0
        return httpx.Response(200, json={"state": True, "data": "any-base64"})

    client = _make_client(handler)
    du = await client.get_download_url("cd5abc", user_agent="TestPlayerUA/1.0")
    assert du.url == direct_url
    assert du.user_agent == "TestPlayerUA/1.0"
    assert du.file_id == file_id
    assert du.file_name == "movie.mkv"
    assert du.file_size == 1073741824
    assert du.sha1 == "SHA1XXX"
    assert du.pickcode == "cd5abc"
    assert du.expires_at == 1800000000
    await client.close()


async def test_get_download_url_requires_pickcode() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="pickcode"):
        await client.get_download_url("", user_agent="ua")
    await client.close()


async def test_get_download_url_requires_user_agent() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="user_agent"):
        await client.get_download_url("cd5xxx", user_agent="")
    await client.close()


async def test_get_download_url_state_false_maps_to_auth_error() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 20130827, "error": "account frozen"
    }))
    with pytest.raises(Cloud115AuthError):
        await client.get_download_url("cd5xxx", user_agent="ua")
    await client.close()


async def test_get_download_url_banned_resource_errno_maps_to_not_found() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 4100008, "error": "banned"
    }))
    with pytest.raises(Cloud115NotFoundError):
        await client.get_download_url("cd5xxx", user_agent="ua")
    await client.close()


async def test_get_download_url_url_zero_maps_to_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端把 url 字段返 0 表示"这是目录 / 被封禁"，等同 NotFound。"""
    fake_decrypted = {
        "8888": {"file_name": "x", "file_size": "0", "pick_code": "p", "sha1": "", "url": 0}
    }
    monkeypatch.setattr(client_module, "decrypt_response", lambda _b64: fake_decrypted)

    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "data": "irrelevant"}))
    with pytest.raises(Cloud115NotFoundError, match="banned or directory"):
        await client.get_download_url("p", user_agent="ua")
    await client.close()


async def test_get_download_url_expires_at_minus_one_when_no_t_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """直链 URL 里没有 t= 参数时 expires_at 返 -1（不抛异常）。"""
    direct_url = "https://cdn.example.com/x.mkv?nothing=1"
    fake_decrypted = {
        "7777": {"file_name": "x", "file_size": "0", "pick_code": "p", "sha1": "",
                 "url": {"url": direct_url}}
    }
    monkeypatch.setattr(client_module, "decrypt_response", lambda _b64: fake_decrypted)

    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "data": "irrelevant"}))
    du = await client.get_download_url("p", user_agent="ua")
    assert du.expires_at == -1
    await client.close()


# ---------------------------------------------------------------------------
# 429 / 5xx / 超时 处理
# ---------------------------------------------------------------------------


async def test_429_raises_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, json={"state": False})

    client = _make_client(handler)
    with pytest.raises(Cloud115RateLimitedError) as info:
        await client.list_dir("0")
    assert info.value.retry_after_seconds == 30
    await client.close()


async def test_429_without_retry_after_yields_none_field() -> None:
    client = _make_client(lambda r: httpx.Response(429, json={"state": False}))
    with pytest.raises(Cloud115RateLimitedError) as info:
        await client.list_dir("0")
    assert info.value.retry_after_seconds is None
    await client.close()


async def test_401_maps_to_auth_error() -> None:
    client = _make_client(lambda r: httpx.Response(401))
    with pytest.raises(Cloud115AuthError):
        await client.list_dir("0")
    await client.close()


async def test_403_maps_to_auth_error() -> None:
    client = _make_client(lambda r: httpx.Response(403))
    with pytest.raises(Cloud115AuthError):
        await client.list_dir("0")
    await client.close()


async def test_5xx_retries_and_succeeds() -> None:
    """前 2 次返 500、第 3 次成功 → 正常返回，验证退避重试逻辑（不真 sleep 等待）。"""
    call_count = 0
    good_payload = {"state": True, "count": 0, "offset": 0, "limit": 50, "data": []}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return httpx.Response(500, content=b"internal error")
        return httpx.Response(200, json=good_payload)

    client = _make_client(handler)
    # Patch asyncio.sleep 加速测试
    import asyncio
    async def instant_sleep(_delay: float) -> None:
        return None
    original_sleep = asyncio.sleep
    asyncio.sleep = instant_sleep  # type: ignore
    try:
        entries, total = await client.list_dir("0")
        assert entries == []
        assert total == 0
        assert call_count == 3  # 一次原请求 + 两次重试
    finally:
        asyncio.sleep = original_sleep  # type: ignore
        await client.close()


async def test_5xx_exhausts_retries_raises_request_error() -> None:
    """连续 3 次 500 → 抛 Cloud115RequestError。"""
    client = _make_client(lambda r: httpx.Response(500, content=b"boom"))
    import asyncio
    async def instant_sleep(_delay: float) -> None:
        return None
    original_sleep = asyncio.sleep
    asyncio.sleep = instant_sleep  # type: ignore
    try:
        with pytest.raises(Cloud115RequestError, match="after"):
            await client.list_dir("0")
    finally:
        asyncio.sleep = original_sleep  # type: ignore
        await client.close()


async def test_non_json_2xx_body_raises_request_error() -> None:
    client = _make_client(lambda r: httpx.Response(200, content=b"<html>oops</html>"))
    with pytest.raises(Cloud115RequestError, match="non-json"):
        await client.list_dir("0")
    await client.close()


# ---------------------------------------------------------------------------
# get_video_info / get_video_segments (VIP-only)
# ---------------------------------------------------------------------------


_MASTER_M3U8_URL = "https://115.com/api/video/m3u8/cd5abc.m3u8"
_VARIANT_M3U8_URL = "https://cpats01.115.com/xyz/HASH_1280.m3u8?u=1&s=4194304"

_SAMPLE_MASTER_M3U8 = (
    "#EXTM3U\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1800000,RESOLUTION=1280x720,NAME="HD"\r\n'
    f"{_VARIANT_M3U8_URL}\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=640x360,NAME="SD"\r\n'
    "https://cpats01.115.com/xyz/HASH_640.m3u8?u=1&s=4194304\r\n"
)

# variant m3u8：3 段，第 2 段用相对路径测 urljoin
_SAMPLE_VARIANT_M3U8 = (
    "#EXTM3U\r\n"
    "#EXT-X-VERSION:3\r\n"
    "#EXT-X-TARGETDURATION:18\r\n"
    "#EXT-X-MEDIA-SEQUENCE:0\r\n"
    "#EXTINF:10.000000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00001.ts?u=1&s=4194304\r\n"
    "#EXTINF:9.500000,\r\n"
    "/xyz/HASH_1280-00002.ts?u=1&s=4194304\r\n"
    "#EXTINF:5.123000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00003.ts?u=1&s=4194304\r\n"
    "#EXT-X-ENDLIST\r\n"
)

_SAMPLE_VIDEO_INFO_JSON = {
    "state": True,
    "video_url": _MASTER_M3U8_URL,
    "video_url_demo": _MASTER_M3U8_URL,
    "thumb_url": "https://static.115.com/video/HASH.jpg",
    "width": "1280",
    "height": "720",
    "file_status": 1,
    "inlay_power": 0,
    "download_url": [],
}


async def test_get_video_info_builds_request_and_parses_definitions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com" and request.url.path == "/files/video":
            assert request.url.params["pickcode"] == "cd5abc"
            return httpx.Response(200, json=_SAMPLE_VIDEO_INFO_JSON)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_MASTER_M3U8.encode("utf-8"))
        raise AssertionError(f"unexpected request: {request.url}")

    client = _make_client(handler)
    info = await client.get_video_info("cd5abc")
    assert info.pickcode == "cd5abc"
    assert info.width == 1280
    assert info.height == 720
    assert info.thumb_url.startswith("https://")
    assert info.master_m3u8_url == _MASTER_M3U8_URL
    assert len(info.definitions) == 2
    # 顺序按 master m3u8 里的出现顺序
    hd, sd = info.definitions
    assert hd.bandwidth == 1800000
    assert hd.resolution == "1280x720"
    assert hd.label == "HD"
    assert hd.m3u8_url == _VARIANT_M3U8_URL
    assert sd.bandwidth == 800000
    assert sd.label == "SD"
    await client.close()


async def test_get_video_info_errno_406_maps_to_membership_required() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 406, "error": "需要VIP会员"
    }))
    with pytest.raises(Cloud115MembershipRequiredError):
        await client.get_video_info("cd5abc")
    await client.close()


async def test_get_video_info_empty_video_url_maps_to_not_found() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": True, "video_url": "", "width": 0, "height": 0
    }))
    with pytest.raises(Cloud115NotFoundError, match="video_url missing"):
        await client.get_video_info("cd5abc")
    await client.close()


async def test_get_video_segments_picks_highest_bandwidth_by_default() -> None:
    """默认挑最高码率的 variant（HD 1800000 > SD 800000）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com" and request.url.path == "/files/video":
            return httpx.Response(200, json=_SAMPLE_VIDEO_INFO_JSON)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_MASTER_M3U8.encode("utf-8"))
        if str(request.url) == _VARIANT_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_VARIANT_M3U8.encode("utf-8"))
        raise AssertionError(f"unexpected request: {request.url}")

    client = _make_client(handler)
    segments = await client.get_video_segments("cd5abc")
    assert len(segments) == 3
    # index 从 0 递增
    assert [s.index for s in segments] == [0, 1, 2]
    # 每段的 duration 精确解析
    assert segments[0].duration_seconds == 10.0
    assert segments[1].duration_seconds == 9.5
    assert abs(segments[2].duration_seconds - 5.123) < 1e-9
    # 相对路径已经拼成绝对 URL
    assert segments[1].url == "https://cpats01.115.com/xyz/HASH_1280-00002.ts?u=1&s=4194304"
    # 首尾段的绝对 URL 直接透传
    assert segments[0].url.endswith("HASH_1280-00001.ts?u=1&s=4194304")
    assert segments[2].url.endswith("HASH_1280-00003.ts?u=1&s=4194304")
    await client.close()


async def test_get_video_segments_prefer_bandwidth_matches_exact() -> None:
    """指定精确 bandwidth 时挑对应 variant。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com" and request.url.path == "/files/video":
            return httpx.Response(200, json=_SAMPLE_VIDEO_INFO_JSON)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_MASTER_M3U8.encode("utf-8"))
        # 期待请求走 SD variant（bandwidth=800000）
        if request.url.host == "cpats01.115.com" and "640" in request.url.path:
            # 返回一个 1 段的 fake variant
            fake = (
                "#EXTM3U\r\n#EXTINF:10.0,\r\n"
                "https://cpats01.115.com/xyz/sd-00001.ts\r\n#EXT-X-ENDLIST\r\n"
            )
            return httpx.Response(200, content=fake.encode("utf-8"))
        raise AssertionError(f"unexpected request: {request.url}")

    client = _make_client(handler)
    segments = await client.get_video_segments("cd5abc", prefer_bandwidth=800000)
    assert len(segments) == 1
    assert "sd-00001.ts" in segments[0].url
    await client.close()


async def test_get_video_segments_prefer_bandwidth_falls_back_to_highest() -> None:
    """指定的 bandwidth 找不到时回退到最高码率（不抛异常）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com" and request.url.path == "/files/video":
            return httpx.Response(200, json=_SAMPLE_VIDEO_INFO_JSON)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_MASTER_M3U8.encode("utf-8"))
        if str(request.url) == _VARIANT_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_VARIANT_M3U8.encode("utf-8"))
        raise AssertionError(f"unexpected request: {request.url}")

    client = _make_client(handler)
    segments = await client.get_video_segments("cd5abc", prefer_bandwidth=9999999)
    # 3 段说明用了 HD variant（不是找不到就抛异常）
    assert len(segments) == 3
    await client.close()


async def test_master_m3u8_parser_handles_empty_input() -> None:
    """master m3u8 为空时返回空 definitions（不抛异常，交给上层 get_video_segments 抛 NotFound）。"""
    from src.lib.cloud115.client import Cloud115Client
    defs = Cloud115Client._parse_master_m3u8("", base_url="https://example.com/x.m3u8")
    assert defs == []


async def test_variant_m3u8_parser_handles_empty_input() -> None:
    from src.lib.cloud115.client import Cloud115Client
    segs = Cloud115Client._parse_variant_m3u8("", base_url="https://example.com/x.m3u8")
    assert segs == []


async def test_close_is_idempotent_for_external_client() -> None:
    """外部注入 client 时 close() 不能关掉外部对象（owned=False）。"""
    external = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"state": True})
    ))
    client = Cloud115Client(cookies=COOKIE, http_client=external)
    await client.close()
    # 外部 client 应仍可用（未被关闭）
    resp = await external.get("https://example.com")
    assert resp.status_code == 200
    await external.aclose()


# ---------------------------------------------------------------------------
# pickcode_info
# ---------------------------------------------------------------------------


async def test_pickcode_info_uses_pick_code_query_param() -> None:
    sample = {
        "state": True,
        "data": [{
            "fid": "222", "cid": "0", "n": "x.mkv", "s": 42,
            "sha": "SHAY", "pc": "pcpc", "te": 1, "tp": 2, "iv": 1,
        }],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "webapi.115.com"
        assert request.url.path == "/files/get_info"
        # 关键：走 pick_code 参数，不是 file_id
        assert request.url.params.get("pick_code") == "pcpc"
        assert "file_id" not in request.url.params
        return httpx.Response(200, json=sample)

    client = _make_client(handler)
    meta = await client.pickcode_info("pcpc")
    assert meta.file_id == "222"
    assert meta.pickcode == "pcpc"
    assert meta.is_video is True
    await client.close()


async def test_pickcode_info_empty_data_raises_not_found() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "data": []}))
    with pytest.raises(Cloud115NotFoundError, match="pick_code"):
        await client.pickcode_info("bogus-pc")
    await client.close()


async def test_pickcode_info_requires_pickcode() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="pickcode"):
        await client.pickcode_info("")
    await client.close()


async def test_file_info_requires_file_id() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="file_id"):
        await client.file_info("")
    await client.close()


# ---------------------------------------------------------------------------
# dir_info
# ---------------------------------------------------------------------------


async def test_dir_info_root_returns_sentinel_without_request() -> None:
    """cid=0 走服务端会 errNo=1001，SDK 直接构造哨兵返回。"""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"state": True})

    client = _make_client(handler)
    d = await client.dir_info("0")
    assert call_count == 0    # 未发起任何请求
    assert d.cid == "0"
    assert d.name == "根目录"
    assert d.pickcode == ""
    assert d.parent_id == ""
    assert d.paths == ()
    assert d.file_count == 0
    await client.close()


async def test_dir_info_parses_response_and_breadcrumb() -> None:
    sample = {
        "state": True,
        "errNo": 0,
        "count": 6,
        "size": "11.23GB",
        "folder_count": 3,
        "show_play_long": 0,
        "play_long": 1893,
        "ctime": 1778749843,
        "utime": 1783841779,
        "file_name": "云下载",
        "pick_code": "fedn5812sugiog3ns8",
        "paths": [
            {"file_id": 0, "file_name": "根目录"},
            {"file_id": 3428707991046116541, "file_name": "父目录"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "webapi.115.com"
        assert request.url.path == "/category/get"
        assert request.url.params.get("cid") == "999888777"
        return httpx.Response(200, json=sample)

    client = _make_client(handler)
    d = await client.dir_info("999888777")
    assert d.cid == "999888777"
    assert d.name == "云下载"
    assert d.pickcode == "fedn5812sugiog3ns8"
    assert d.file_count == 6
    assert d.folder_count == 3
    assert d.play_long_seconds == 1893
    assert d.mtime == 1783841779
    assert d.ctime == 1778749843
    # 面包屑：从根开始
    assert len(d.paths) == 2
    assert d.paths[0].file_id == "0"        # 根 file_id 是数字 0，字符串化后应为 "0"
    assert d.paths[0].name == "根目录"
    assert d.paths[1].file_id == "3428707991046116541"
    # parent_id 从 paths 末尾解析
    assert d.parent_id == "3428707991046116541"
    await client.close()


async def test_dir_info_state_false_maps_errno_1001_to_base_error() -> None:
    """cid 无效时服务端返 errNo=1001；1001 不在已知子类集合里，落到基类。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errNo": 1001, "errno": 1001, "error": "参数错误"
    }))
    with pytest.raises(Cloud115Error) as info:
        await client.dir_info("bogus-cid")
    assert type(info.value) is Cloud115Error
    await client.close()


async def test_dir_info_requires_cid() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="cid"):
        await client.dir_info("")
    await client.close()


# ---------------------------------------------------------------------------
# get_video_info: file_status 处理路径
# ---------------------------------------------------------------------------


async def test_get_video_info_file_status_0_raises_not_ready() -> None:
    """转码未完成（file_status=0）时抛 VideoNotReadyError，不是 NotFound。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": True,
        "file_status": 0,
        "video_url": "",
        "width": 0,
        "height": 0,
    }))
    with pytest.raises(Cloud115VideoNotReadyError) as info:
        await client.get_video_info("cd5xxx")
    assert info.value.file_status == 0
    await client.close()


async def test_get_video_info_file_status_2_raises_not_ready() -> None:
    """任何非 1 的 file_status 都视为未就绪。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": True, "file_status": 2, "video_url": "", "width": 0, "height": 0,
    }))
    with pytest.raises(Cloud115VideoNotReadyError) as info:
        await client.get_video_info("cd5xxx")
    assert info.value.file_status == 2
    await client.close()


async def test_get_video_info_missing_file_status_falls_back_to_ready() -> None:
    """老响应没有 file_status 字段时按 ready 处理，避免破坏兼容。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com" and request.url.path == "/files/video":
            # 无 file_status，只有 video_url
            return httpx.Response(200, json={
                "state": True,
                "video_url": _MASTER_M3U8_URL,
                "width": 1280,
                "height": 720,
            })
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_SAMPLE_MASTER_M3U8.encode("utf-8"))
        raise AssertionError(f"unexpected: {request.url}")

    client = _make_client(handler)
    info = await client.get_video_info("cd5abc")
    assert info.width == 1280
    await client.close()


# ---------------------------------------------------------------------------
# Cookies 保活：Set-Cookie merge + snapshot + update_cookies
# ---------------------------------------------------------------------------


async def test_set_cookie_merged_into_snapshot() -> None:
    """响应带 Set-Cookie 时，snapshot_cookies 应含新字段。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": True},
            headers={"Set-Cookie": "acw_tc=NEWVALUE123;path=/;HttpOnly;Max-Age=1800"},
        )

    client = _make_client(handler, cookies=COOKIE)
    assert "acw_tc=" not in client.snapshot_cookies()
    await client.check_cookies_alive()
    snap = client.snapshot_cookies()
    assert "acw_tc=NEWVALUE123" in snap
    await client.close()


async def test_set_cookie_updates_existing_key() -> None:
    """已存在的 key 被服务端覆盖时用新值。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": True, "count": 0, "data": []},
            headers={"Set-Cookie": "SEID=REFRESHED;path=/;HttpOnly"},
        )

    client = _make_client(handler, cookies=COOKIE)
    assert "SEID=xyz" in client.snapshot_cookies()
    await client.list_dir("0")
    snap = client.snapshot_cookies()
    assert "SEID=REFRESHED" in snap
    assert "SEID=xyz" not in snap
    await client.close()


async def test_next_request_carries_refreshed_cookie() -> None:
    """服务端塞了新 acw_tc 后，下一次请求的 Cookie header 必须带上新值（跨子域也认账）。"""
    captured: list[str] = []
    step = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        step["count"] += 1
        captured.append(request.headers.get("Cookie", ""))
        if step["count"] == 1:
            # 第一次：塞 acw_tc
            return httpx.Response(200, json={"state": True}, headers={
                "Set-Cookie": "acw_tc=FIRST;path=/;Max-Age=1800",
            })
        # 第二次：应该带着 acw_tc=FIRST 过来
        return httpx.Response(200, json={"state": True, "count": 0, "data": []})

    client = _make_client(handler, cookies=COOKIE)
    await client.check_cookies_alive()
    await client.list_dir("0")
    assert step["count"] == 2
    assert "acw_tc=" not in captured[0]
    assert "acw_tc=FIRST" in captured[1]
    await client.close()


async def test_snapshot_preserves_original_insertion_order() -> None:
    """snapshot 保持原 cookies 里字段的顺序（服务端一般不校验 Cookie 顺序，
    但保序是 SDK 契约里"逐字节透传"精神的延续）。"""
    client = _make_client(
        lambda r: httpx.Response(200, json={"state": True}),
        cookies="GST=g; UID=1_A1_100; CID=c; SEID=s; KID=k",
    )
    snap = client.snapshot_cookies()
    assert snap == "GST=g; UID=1_A1_100; CID=c; SEID=s; KID=k"
    await client.close()


async def test_update_cookies_replaces_state() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True}))
    assert client.user_id == USER_ID
    new_cookies = "UID=87654321_A1_1800000000; CID=other; SEID=new"
    client.update_cookies(new_cookies)
    assert client.user_id == "87654321"
    assert "SEID=new" in client.snapshot_cookies()
    # 老 cookies 里的字段不应残留
    assert "SEID=xyz" not in client.snapshot_cookies()
    await client.close()


async def test_update_cookies_rejects_without_uid_and_preserves_state() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True}))
    original = client.snapshot_cookies()
    with pytest.raises(Cloud115AuthError):
        client.update_cookies("CID=x; SEID=y")
    # 原状态未被破坏
    assert client.user_id == USER_ID
    assert client.snapshot_cookies() == original
    await client.close()


async def test_user_id_property_matches_uid_cookie() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True}))
    assert client.user_id == USER_ID
    await client.close()


# ---------------------------------------------------------------------------
# 网络异常在业务接口的重试路径（补 list_dir / file_info 覆盖）
# ---------------------------------------------------------------------------


async def test_list_dir_retries_on_timeout_and_succeeds() -> None:
    """list_dir 遇到 TimeoutException 走 _request 的重试逻辑。"""
    call_count = 0
    good = {"state": True, "count": 0, "offset": 0, "limit": 50, "data": []}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectTimeout("mock timeout")
        return httpx.Response(200, json=good)

    client = _make_client(handler)
    import asyncio
    orig = asyncio.sleep
    async def instant(_): return None
    asyncio.sleep = instant  # type: ignore
    try:
        entries, total = await client.list_dir("0")
        assert entries == []
        assert call_count == 2
    finally:
        asyncio.sleep = orig  # type: ignore
        await client.close()


async def test_file_info_network_error_raises_after_retries() -> None:
    """file_info 连续网络错重试耗尽后抛 Cloud115RequestError。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("mock connect fail")

    client = _make_client(handler)
    import asyncio
    orig = asyncio.sleep
    async def instant(_): return None
    asyncio.sleep = instant  # type: ignore
    try:
        with pytest.raises(Cloud115RequestError, match="after"):
            await client.file_info("111")
    finally:
        asyncio.sleep = orig  # type: ignore
        await client.close()


# ---------------------------------------------------------------------------
# list_dir offset 透传断言（原来 handler 只校验 offset==0）
# ---------------------------------------------------------------------------


async def test_list_dir_offset_is_passed_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["offset"] == "500"
        assert request.url.params["limit"] == "100"
        return httpx.Response(200, json={"state": True, "count": 0, "data": []})

    client = _make_client(handler)
    entries, total = await client.list_dir("0", offset=500, limit=100)
    assert entries == []
    await client.close()


# ---------------------------------------------------------------------------
# m3u8 边界：无 BANDWIDTH / 无 RESOLUTION / EXTINF 整数时长
# ---------------------------------------------------------------------------


async def test_master_m3u8_parser_missing_bandwidth_defaults_to_zero() -> None:
    from src.lib.cloud115.client import Cloud115Client
    text = (
        "#EXTM3U\r\n"
        '#EXT-X-STREAM-INF:PROGRAM-ID=1,RESOLUTION=1280x720,NAME="HD"\r\n'
        "https://example.com/variant.m3u8\r\n"
    )
    defs = Cloud115Client._parse_master_m3u8(text, base_url="https://example.com/master.m3u8")
    assert len(defs) == 1
    assert defs[0].bandwidth == 0
    assert defs[0].resolution == "1280x720"


async def test_master_m3u8_parser_missing_resolution_and_name() -> None:
    from src.lib.cloud115.client import Cloud115Client
    text = (
        "#EXTM3U\r\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=500000\r\n"
        "https://example.com/variant.m3u8\r\n"
    )
    defs = Cloud115Client._parse_master_m3u8(text, base_url="https://example.com/master.m3u8")
    assert len(defs) == 1
    assert defs[0].bandwidth == 500000
    assert defs[0].resolution == ""
    assert defs[0].label == ""


async def test_variant_m3u8_parser_accepts_integer_extinf() -> None:
    from src.lib.cloud115.client import Cloud115Client
    text = (
        "#EXTM3U\r\n"
        "#EXTINF:10,\r\n"
        "https://example.com/a.ts\r\n"
        "#EXTINF:9,\r\n"
        "https://example.com/b.ts\r\n"
        "#EXT-X-ENDLIST\r\n"
    )
    segs = Cloud115Client._parse_variant_m3u8(text, base_url="https://example.com/x.m3u8")
    assert len(segs) == 2
    assert segs[0].duration_seconds == 10.0
    assert segs[1].duration_seconds == 9.0


# ===========================================================================
# 离线下载：list_offline_tasks / offline_quota / default_download_dir /
#          add_offline_urls / delete_offline_tasks / clear_offline_tasks /
#          restart_offline_task
# ===========================================================================


_SAMPLE_TASK_LISTS = {
    "state": True,
    "page": 1,
    "page_count": 3,
    "page_size": 30,
    "count": 30,
    "quota": 197,
    "total": 200,
    "tasks": [
        {
            "info_hash": "aaaa" * 10,
            "add_time": 1700000000,
            "percentDone": 100,
            "display_percent": 100,
            "size": 1234567,
            "peers": 0,
            "rateDownload": 0,
            "name": "Movie.mkv",
            "last_update": 1700001000,
            "left_time": 0,
            "file_id": "999888777",
            "pick_code": "pcabc",
            "wp_path_id": "555",
            "url": "",
            "move": 1,
            "status": 2,
            "status_text": "下载成功",
            "display_status": "finished",
            "retry_count": 0,
            "retry_limit": 3,
        },
        {
            "info_hash": "bbbb" * 10,
            "add_time": 1700100000,
            "percentDone": 42.5,
            "size": 2000000000,
            "peers": 15,
            "rateDownload": 3000000,     # 3 MB/s
            "name": "Series.S01",
            "last_update": 1700102000,
            "left_time": 300,
            "file_id": "",              # 未完成还没落地
            "pick_code": "",
            "wp_path_id": "555",
            "url": "magnet:?xt=urn:btih:bbbb...",
            "status": 1,
            "status_text": "下载中",
            "retry_count": 1,
            "retry_limit": 3,
        },
    ],
}


async def test_list_offline_tasks_builds_request_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "115.com"
        assert request.url.path == "/web/lixian/"
        assert request.url.params["ct"] == "lixian"
        assert request.url.params["ac"] == "task_lists"
        assert request.url.params["page"] == "2"
        assert request.url.params["page_size"] == "20"
        return httpx.Response(200, json=_SAMPLE_TASK_LISTS)

    client = _make_client(handler)
    page = await client.list_offline_tasks(page=2, page_size=20)
    assert page.page == 1  # 响应字段直接透传，测试样本里是 1
    assert page.page_count == 3
    assert page.total_tasks == 200
    assert len(page.tasks) == 2

    # 完成的任务
    t0 = page.tasks[0]
    assert t0.info_hash == "aaaa" * 10
    assert t0.name == "Movie.mkv"
    assert t0.status == 2
    assert t0.status_text == "下载成功"
    assert t0.percent_done == 100.0
    assert t0.file_id == "999888777"
    assert t0.pickcode == "pcabc"
    assert t0.save_dir_id == "555"

    # 进行中的任务
    t1 = page.tasks[1]
    assert t1.status == 1
    assert t1.status_text == "下载中"
    assert t1.percent_done == 42.5
    assert t1.rate_download == 3000000
    assert t1.peers == 15
    assert t1.left_time_seconds == 300
    assert t1.file_id == ""          # 未完成没有 file_id
    assert t1.pickcode == ""
    assert t1.source_url.startswith("magnet:")
    assert t1.retry_count == 1

    await client.close()


async def test_list_offline_tasks_rejects_page_less_than_1() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="page"):
        await client.list_offline_tasks(page=0)
    await client.close()


async def test_list_offline_tasks_rejects_page_size_less_than_1() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="page_size"):
        await client.list_offline_tasks(page_size=0)
    await client.close()


async def test_list_offline_tasks_state_false_maps_to_error() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 990009, "error": "not logged in"
    }))
    with pytest.raises(Cloud115AuthError):
        await client.list_offline_tasks()
    await client.close()


async def test_offline_quota_extracts_total_and_remaining() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # quota 通过 task_lists 端点获取（page=1, page_size=1 减轻负载）
        assert request.url.path == "/web/lixian/"
        assert request.url.params["ac"] == "task_lists"
        assert request.url.params["page"] == "1"
        assert request.url.params["page_size"] == "1"
        return httpx.Response(200, json={
            "state": True, "total": 200, "quota": 197,
            "page": 1, "page_count": 200, "page_size": 1, "tasks": [],
        })

    client = _make_client(handler)
    q = await client.offline_quota()
    assert q.total == 200
    assert q.remaining == 197
    await client.close()


async def test_default_download_dir_picks_selected_entry() -> None:
    sample = {
        "state": True,
        "error": None,
        "errno": None,
        "data": [
            {"id": "1", "user_id": "u", "file_id": "111", "update_time": "1000",
             "is_selected": "0", "file_name": "候选 A"},
            {"id": "2", "user_id": "u", "file_id": "222", "update_time": "2000",
             "is_selected": "1", "file_name": "云下载"},   # <- 挑这个
            {"id": "3", "user_id": "u", "file_id": "333", "update_time": "3000",
             "is_selected": "0", "file_name": "候选 C"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "webapi.115.com"
        # 注意 115 端点名字缺一个 l：offine，不是 offline
        assert request.url.path == "/offine/downpath"
        return httpx.Response(200, json=sample)

    client = _make_client(handler)
    d = await client.default_download_dir()
    assert d.entry_id == "222"
    assert d.name == "云下载"
    assert d.is_dir is True
    assert d.mtime == 2000
    await client.close()


async def test_default_download_dir_falls_back_to_first_when_none_selected() -> None:
    """如果没有 is_selected=1 的候选，取第一个（服务端不给标就默认第一个）。"""
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": True,
        "data": [{"file_id": "AAA", "file_name": "第一个", "update_time": "0",
                  "is_selected": "0"}],
    }))
    d = await client.default_download_dir()
    assert d.entry_id == "AAA"
    await client.close()


async def test_default_download_dir_raises_not_found_when_no_candidates() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={"state": True, "data": []}))
    with pytest.raises(Cloud115NotFoundError, match="no default"):
        await client.default_download_dir()
    await client.close()


async def test_add_offline_urls_builds_form_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/web/lixian/"
        assert request.url.params["ct"] == "lixian"
        assert request.url.params["ac"] == "add_task_urls"
        assert request.method == "POST"
        # form body 里应有 url[0], url[1], wp_path_id
        body = request.content.decode("utf-8")
        from urllib.parse import parse_qs
        form = parse_qs(body)
        assert form["url[0]"] == ["magnet:?xt=urn:btih:aaa"]
        assert form["url[1]"] == ["http://example.com/x.mp4"]
        assert form["wp_path_id"] == ["555"]
        return httpx.Response(200, json={
            "state": True, "errno": 0,
            "result": [
                {"info_hash": "aaa" * 13 + "a", "url": "magnet:?xt=urn:btih:aaa"},
                {"info_hash": "bbb" * 13 + "b", "url": "http://example.com/x.mp4"},
            ],
        })

    client = _make_client(handler)
    results = await client.add_offline_urls(
        ["magnet:?xt=urn:btih:aaa", "http://example.com/x.mp4"],
        save_dir_id="555",
    )
    assert len(results) == 2
    assert results[0].info_hash == "aaa" * 13 + "a"
    assert results[0].url == "magnet:?xt=urn:btih:aaa"
    assert results[1].url == "http://example.com/x.mp4"
    await client.close()


async def test_add_offline_urls_rejects_empty_urls() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="urls"):
        await client.add_offline_urls([], save_dir_id="555")
    await client.close()


async def test_add_offline_urls_rejects_empty_save_dir_id() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="save_dir_id"):
        await client.add_offline_urls(["magnet:?xt=urn:btih:aaa"], save_dir_id="")
    await client.close()


async def test_add_offline_urls_quota_exceeded_maps_to_specific_error() -> None:
    client = _make_client(lambda r: httpx.Response(200, json={
        "state": False, "errno": 10008, "error": "离线数已达上限"
    }))
    with pytest.raises(Cloud115OfflineQuotaExceededError) as info:
        await client.add_offline_urls(["magnet:?xt=urn:btih:aaa"], save_dir_id="555")
    assert info.value.errno == 10008
    await client.close()


async def test_delete_offline_tasks_builds_hash_array_and_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ac"] == "task_del"
        body = request.content.decode("utf-8")
        from urllib.parse import parse_qs
        form = parse_qs(body)
        assert form["hash[0]"] == ["hashA"]
        assert form["hash[1]"] == ["hashB"]
        assert form["flag"] == ["1"]     # delete_source_files=True
        return httpx.Response(200, json={"state": True})

    client = _make_client(handler)
    await client.delete_offline_tasks(["hashA", "hashB"], delete_source_files=True)
    await client.close()


async def test_delete_offline_tasks_flag_defaults_to_0() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        assert "flag=0" in body    # 默认 delete_source_files=False
        return httpx.Response(200, json={"state": True})

    client = _make_client(handler)
    await client.delete_offline_tasks(["h"])
    await client.close()


async def test_delete_offline_tasks_rejects_empty_hashes() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="info_hashes"):
        await client.delete_offline_tasks([])
    await client.close()


@pytest.mark.parametrize("scope,expected_flag", [
    ("finished", "0"),
    ("all", "1"),
    ("failed", "2"),
    ("running", "3"),
    ("finished_with_source", "4"),
    ("all_with_source", "5"),
])
async def test_clear_offline_tasks_scope_to_flag_mapping(scope, expected_flag) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ac"] == "task_clear"
        body = request.content.decode("utf-8")
        assert f"flag={expected_flag}" in body
        return httpx.Response(200, json={"state": True})

    client = _make_client(handler)
    await client.clear_offline_tasks(scope=scope)
    await client.close()


async def test_clear_offline_tasks_rejects_unknown_scope() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="scope"):
        await client.clear_offline_tasks(scope="whatever")  # type: ignore
    await client.close()


async def test_restart_offline_task_sends_info_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ac"] == "restart"
        body = request.content.decode("utf-8")
        assert "info_hash=THE_HASH" in body
        return httpx.Response(200, json={"state": True})

    client = _make_client(handler)
    await client.restart_offline_task("THE_HASH")
    await client.close()


async def test_restart_offline_task_rejects_empty_hash() -> None:
    client = _make_client(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="info_hash"):
        await client.restart_offline_task("")
    await client.close()
