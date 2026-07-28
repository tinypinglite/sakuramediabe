import httpx
import pytest

from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115Client,
    Cloud115RequestError,
    Cloud115RiskControlError,
    DirectUrl,
)

MOCK_COOKIES = "UID=12345_A1_1700000000; CID=cid; SEID=seid"


async def test_cloud115_session_update_is_atomic_on_invalid_cookies() -> None:
    client = Cloud115Client(MOCK_COOKIES)
    original = client.snapshot_cookies()

    with pytest.raises(Cloud115AuthError):
        client.update_cookies("CID=missing-uid")

    assert client.snapshot_cookies() == original
    await client.close()


async def test_probe_merges_set_cookie_into_session_snapshot() -> None:
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"state": True},
                headers={"Set-Cookie": "acw_tc=fresh; Max-Age=1800; Path=/"},
            )
        )
    )
    client = Cloud115Client(MOCK_COOKIES, http_client=async_client)
    try:
        assert await client.check_cookies_alive() is True
        assert "acw_tc=fresh" in client.snapshot_cookies()
    finally:
        await async_client.aclose()


async def test_transport_retries_get_but_not_post() -> None:
    get_attempts = 0

    def get_handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_attempts
        get_attempts += 1
        if get_attempts == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"state": True, "data": [], "count": 0})

    get_http_client = httpx.AsyncClient(transport=httpx.MockTransport(get_handler))
    get_client = Cloud115Client(MOCK_COOKIES, http_client=get_http_client)
    try:
        assert await get_client.list_dir("0") == ([], 0)
        assert get_attempts == 2
    finally:
        await get_http_client.aclose()

    post_attempts = 0

    def post_handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        post_attempts += 1
        return httpx.Response(500, text="temporary")

    post_http_client = httpx.AsyncClient(transport=httpx.MockTransport(post_handler))
    post_client = Cloud115Client(MOCK_COOKIES, http_client=post_http_client)
    try:
        with pytest.raises(Cloud115RequestError):
            await post_client.mkdir("0", "target")
        assert post_attempts == 1
    finally:
        await post_http_client.aclose()


async def test_transport_maps_waf_405_to_risk_control() -> None:
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(405, text="blocked")
        )
    )
    client = Cloud115Client(MOCK_COOKIES, http_client=async_client)
    try:
        with pytest.raises(Cloud115RiskControlError):
            await client.list_dir("0")
    finally:
        await async_client.aclose()


async def test_client_does_not_close_injected_http_client() -> None:
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"state": True})
        )
    )
    client = Cloud115Client(MOCK_COOKIES, http_client=async_client)

    await client.close()

    assert async_client.is_closed is False
    await async_client.aclose()


async def test_download_bytes_uses_capability_transport_client(monkeypatch) -> None:
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"subtitle")
        )
    )
    client = Cloud115Client(MOCK_COOKIES, http_client=async_client)

    async def _direct_url(*_args, **_kwargs) -> DirectUrl:
        return DirectUrl(
            file_id="1",
            file_name="subtitle.srt",
            file_size=8,
            sha1="",
            pickcode="pc",
            url="https://download.example/subtitle.srt",
            user_agent="test-agent",
            expires_at=-1,
        )

    monkeypatch.setattr(client.playback, "get_download_url", _direct_url)
    try:
        assert (
            await client.download_bytes(
                "pc",
                user_agent="test-agent",
                max_bytes=32,
            )
            == b"subtitle"
        )
    finally:
        await async_client.aclose()


async def test_default_download_dir_builds_dir_entry() -> None:
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "state": True,
                    "data": [
                        {
                            "file_id": "42",
                            "file_name": "云下载",
                            "is_selected": 1,
                            "update_time": 123,
                        }
                    ],
                },
            )
        )
    )
    client = Cloud115Client(MOCK_COOKIES, http_client=async_client)
    try:
        entry = await client.default_download_dir()
        assert entry.entry_id == "42"
        assert entry.name == "云下载"
        assert entry.is_dir is True
    finally:
        await async_client.aclose()


async def test_batch_pacing_rests_after_configured_request_count(monkeypatch) -> None:
    """每打满 N 个 webapi 请求就长休一次：匀速闸门管不住累计请求量。"""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("src.lib.cloud115.transport.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "src.lib.cloud115.transport.random.uniform", lambda lo, hi: hi
    )

    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"state": True, "data": []})
        )
    )
    waits: list[float] = []
    client = Cloud115Client(
        MOCK_COOKIES,
        http_client=async_client,
        min_request_interval=0.0,
        batch_rest_every=3,
        batch_rest_min_seconds=10.0,
        batch_rest_max_seconds=20.0,
        on_pace_wait=waits.append,
    )
    try:
        for _ in range(4):
            await client.list_dir("0", limit=10)
    finally:
        await client.close()

    # 前 3 个请求不等待；第 3 个请求后预定的长休由第 4 个请求吃掉。
    # 实际等待 = 预定时刻 - 当前时刻，会被真实耗时抵掉零点几毫秒。
    assert len(slept) == 1
    assert slept[0] == pytest.approx(20.0, abs=0.5)
    assert waits == slept


async def test_batch_pacing_ignores_non_risk_controlled_hosts(monkeypatch) -> None:
    """取直链走 proapi，不在 WAF 风控域，不该推高批次计数。"""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("src.lib.cloud115.transport.asyncio.sleep", fake_sleep)

    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"state": True, "data": {"url": {"url": "https://cdn/x"}}}
            )
        )
    )
    client = Cloud115Client(
        MOCK_COOKIES,
        http_client=async_client,
        min_request_interval=0.0,
        batch_rest_every=2,
        batch_rest_min_seconds=10.0,
        batch_rest_max_seconds=10.0,
    )
    try:
        for _ in range(4):
            await client.transport._request_json(
                "GET", "https://proapi.115.com/app/chrome/downurl"
            )
    finally:
        await client.close()

    assert slept == []


async def test_batch_pacing_disabled_by_default(monkeypatch) -> None:
    """交互路径默认关闭批次休息，绝不能让用户等待的请求撞上长停顿。"""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("src.lib.cloud115.transport.asyncio.sleep", fake_sleep)

    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"state": True, "data": []})
        )
    )
    client = Cloud115Client(
        MOCK_COOKIES, http_client=async_client, min_request_interval=0.0
    )
    try:
        for _ in range(5):
            await client.list_dir("0", limit=10)
    finally:
        await client.close()

    assert slept == []
