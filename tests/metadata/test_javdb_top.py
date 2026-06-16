import pytest

import src.metadata._providers.http_client as http_client_module
from src.metadata._providers.exceptions import JavdbAuthError, MetadataRequestError
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
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, data=None, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "data": data})
        return self._responses.pop(0)


def _make_provider(monkeypatch, responses, *, username="user@example.com", password="secret"):
    fake = FakeHttpClient(responses)
    monkeypatch.setattr(http_client_module.httpx, "Client", lambda **kwargs: fake)
    provider = JavdbProvider(host="example.com", username=username, password=password)
    return provider, fake


def _movies(*numbers):
    return {"success": 1, "data": {"movies": [{"number": n} for n in numbers]}}


def test_get_top_numbers_logs_in_then_paginates_until_empty(monkeypatch):
    provider, fake = _make_provider(
        monkeypatch,
        [
            FakeResponse({"data": {"token": "tok-1"}}),  # 登录
            FakeResponse(_movies("ABP-001", "ABP-002")),  # page1
            FakeResponse(_movies("ABP-003")),             # page2
            FakeResponse({"success": 1, "data": {"movies": []}}),  # page3 空 -> 停
        ],
    )

    numbers = provider.get_top_numbers(top_type="year", type_value="2024")
    assert numbers == ["ABP-001", "ABP-002", "ABP-003"]

    # 登录一次 + 翻 3 页（第 3 页空即停）。
    assert sum(1 for c in fake.calls if c["url"].endswith("/api/v1/sessions")) == 1
    top_calls = [c for c in fake.calls if "/api/v1/movies/top" in c["url"]]
    assert len(top_calls) == 3
    assert "type=year" in top_calls[0]["url"] and "type_value=2024" in top_calls[0]["url"]
    assert top_calls[0]["headers"]["authorization"] == "Bearer tok-1"


def test_get_top_numbers_respects_max_pages(monkeypatch):
    provider, fake = _make_provider(
        monkeypatch,
        [
            FakeResponse({"data": {"token": "tok-1"}}),
            FakeResponse(_movies("A-1")),
            FakeResponse(_movies("A-2")),
        ],
    )

    numbers = provider.get_top_numbers(top_type="all", type_value="", max_pages=2)
    assert numbers == ["A-1", "A-2"]
    top_calls = [c for c in fake.calls if "/api/v1/movies/top" in c["url"]]
    assert len(top_calls) == 2  # 满 max_pages 即停，不再翻第 3 页


def test_get_top_numbers_requires_login(monkeypatch):
    provider, fake = _make_provider(monkeypatch, [], username=None, password=None)

    with pytest.raises(JavdbAuthError):
        provider.get_top_numbers(top_type="year", type_value="2024")
    assert fake.calls == []  # 未配账号不发起任何请求


def test_get_top_numbers_raises_when_success_not_one(monkeypatch):
    provider, _ = _make_provider(
        monkeypatch,
        [
            FakeResponse({"data": {"token": "tok-1"}}),
            FakeResponse({"success": 0, "message": "請登錄帳號"}),
        ],
    )

    with pytest.raises(MetadataRequestError):
        provider.get_top_numbers(top_type="year", type_value="2024")


def test_get_top_numbers_rejects_unknown_type(monkeypatch):
    provider, fake = _make_provider(monkeypatch, [])

    with pytest.raises(ValueError):
        provider.get_top_numbers(top_type="bogus", type_value="")
    assert fake.calls == []
