from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.service.system.announcement_service import AnnouncementService

FEED_URL = "https://example.test/announcements.json"
NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


def _item(
    item_id: str,
    *,
    level: str = "info",
    published_at: datetime = NOW - timedelta(minutes=5),
    expires_at: datetime | None = None,
    component: str = "all",
    link: str | None = None,
) -> dict:
    return {
        "id": item_id,
        "level": level,
        "title_i18n": {"zh-CN": "标题", "en": "Title"},
        "body_i18n": {"zh-CN": "正文", "en": "Body"},
        "published_at": published_at.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "component": component,
        "link": link,
    }


def _feed(*items: dict) -> dict:
    return {
        "schema_version": 1,
        "updated_at": NOW.isoformat(),
        "announcements": list(items),
    }


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _service(handler, clock: _Clock | None = None) -> AnnouncementService:
    return AnnouncementService(
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(handler),
        ),
        clock=clock or _Clock(),
    )


@pytest.fixture(autouse=True)
def _feed_url(monkeypatch):
    monkeypatch.setenv(AnnouncementService.FEED_URL_ENV_KEY, FEED_URL)
    monkeypatch.delenv(AnnouncementService.ENABLED_ENV_KEY, raising=False)


def test_returns_only_active_announcements_and_reuses_cache():
    calls = 0
    feed = _feed(
        _item("active"),
        _item(
            "expired",
            published_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(minutes=1),
        ),
        _item("future", published_at=NOW + timedelta(minutes=1)),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=feed)

    service = _service(handler)

    assert [item.id for item in service.get_active().items] == ["active"]
    assert [item.id for item in service.get_active().items] == ["active"]
    assert calls == 1


def test_invalid_and_duplicate_entries_are_skipped():
    invalid_i18n = _item("invalid-i18n")
    invalid_i18n["title_i18n"] = {"zh-CN": "只有中文"}
    invalid_link = _item("invalid-link", link="http://example.test/notice")
    invalid_host = _item("invalid-host", link="https://")
    feed = _feed(
        _item("valid"),
        invalid_i18n,
        invalid_link,
        invalid_host,
        _item("valid"),
    )

    service = _service(lambda request: httpx.Response(200, json=feed))

    assert [item.id for item in service.get_active().items] == ["valid"]


def test_etag_304_extends_cache():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json=_feed(_item("active")),
                headers={"ETag": '"v1"'},
            )
        return httpx.Response(304, headers={"ETag": '"v1"'})

    clock = _Clock()
    service = _service(handler, clock)

    assert len(service.get_active().items) == 1
    clock.value = NOW + timedelta(minutes=31)
    assert len(service.get_active().items) == 1

    assert len(calls) == 2
    assert calls[1].headers["If-None-Match"] == '"v1"'


def test_failure_keeps_stale_cache_and_retries_after_delay():
    clock = _Clock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=_feed(_item("stale")))
        return httpx.Response(500)

    service = _service(handler, clock)

    assert [item.id for item in service.get_active().items] == ["stale"]
    clock.value = NOW + timedelta(minutes=31)
    assert [item.id for item in service.get_active().items] == ["stale"]
    assert calls == 2

    clock.value = NOW + timedelta(minutes=36)
    assert [item.id for item in service.get_active().items] == ["stale"]
    assert calls == 3


def test_disabled_service_does_not_request_feed(monkeypatch):
    monkeypatch.setenv(AnnouncementService.ENABLED_ENV_KEY, "false")
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_feed(_item("active")))

    service = _service(handler)

    assert service.get_active().items == []
    assert called is False


def test_unsupported_schema_version_returns_empty_feed():
    feed = _feed(_item("active"))
    feed["schema_version"] = 2

    service = _service(lambda request: httpx.Response(200, json=feed))

    assert service.get_active().items == []


def test_first_request_failure_returns_empty_without_raising():
    service = _service(lambda request: httpx.Response(500))

    assert service.get_active().items == []


def test_malformed_json_returns_empty_without_raising():
    service = _service(
        lambda request: httpx.Response(
            200,
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
    )

    assert service.get_active().items == []
