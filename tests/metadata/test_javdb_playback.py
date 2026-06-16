import pytest

import src.metadata._providers.http_client as http_client_module
from src.metadata._providers.exceptions import MetadataRequestError
from src.metadata._providers.javdb import JavdbProvider


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self) -> dict:
        return self._payload


class FakeHttpClient:
    """按顺序返回预置响应，并记录每次请求参数，便于断言。"""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, data=None, params=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "data": data,
                "params": params,
            }
        )
        return self._responses.pop(0)


def _make_provider(monkeypatch, responses):
    fake = FakeHttpClient(responses)
    monkeypatch.setattr(http_client_module.httpx, "Client", lambda **kwargs: fake)
    provider = JavdbProvider(host="example.com")
    return provider, fake


def test_get_playback_rank_numbers_fetches_and_parses(monkeypatch):
    provider, fake = _make_provider(
        monkeypatch,
        [FakeResponse({"success": 1, "data": {"movies": [{"number": "ABP-001"}, {"number": "ABP-002"}]}})],
    )

    numbers = provider.get_playback_rank_numbers(filter_by="all", period="daily")
    assert numbers == ["ABP-001", "ABP-002"]

    # 仅一次 GET，无登录、无 Bearer 头。
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert "filter_by=all" in call["url"] and "period=daily" in call["url"]
    assert "authorization" not in {k.lower() for k in call["headers"]}
    assert "jdsignature" in call["headers"]


def test_get_playback_rank_numbers_raises_when_success_not_one(monkeypatch):
    provider, _ = _make_provider(
        monkeypatch,
        [FakeResponse({"success": 0, "message": "boom"})],
    )

    with pytest.raises(MetadataRequestError):
        provider.get_playback_rank_numbers(filter_by="high_score", period="weekly")


def test_get_playback_rank_numbers_raises_when_movies_missing(monkeypatch):
    provider, _ = _make_provider(
        monkeypatch,
        [FakeResponse({"success": 1, "data": {}})],
    )

    with pytest.raises(MetadataRequestError):
        provider.get_playback_rank_numbers(filter_by="high_score", period="weekly")


def test_get_playback_rank_numbers_rejects_unknown_filter(monkeypatch):
    provider, fake = _make_provider(monkeypatch, [])

    with pytest.raises(ValueError):
        provider.get_playback_rank_numbers(filter_by="unknown", period="daily")
    assert fake.calls == []  # 参数非法时不应发起任何请求
