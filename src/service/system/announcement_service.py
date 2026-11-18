from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger
from pydantic import ValidationError

from src.schema.system.announcements import (
    StatusAnnouncementResource,
    StatusAnnouncementsResource,
)


class AnnouncementService:
    DEFAULT_FEED_URL = (
        "https://tinypinglite.github.io/sakuramedia/announcements.json"
    )
    FEED_URL_ENV_KEY = "SAKURAMEDIA_ANNOUNCEMENTS_URL"
    ENABLED_ENV_KEY = "SAKURAMEDIA_ANNOUNCEMENTS_ENABLED"
    SUPPORTED_SCHEMA_VERSION = 1
    CACHE_TTL = timedelta(minutes=30)
    RETRY_DELAY = timedelta(minutes=5)
    REQUEST_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client_factory = client_factory or self._default_client
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._cached_items: list[StatusAnnouncementResource] | None = None
        self._cache_expires_at: datetime | None = None
        self._next_attempt_at: datetime | None = None
        self._etag: str | None = None

    def _default_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )

    @property
    def feed_url(self) -> str:
        return (
            os.getenv(self.FEED_URL_ENV_KEY, "").strip()
            or self.DEFAULT_FEED_URL
        )

    def is_enabled(self) -> bool:
        return (
            os.getenv(self.ENABLED_ENV_KEY, "").strip().lower()
            not in {"false", "0", "no", "off"}
        )

    def get_active(self) -> StatusAnnouncementsResource:
        if not self.is_enabled():
            return StatusAnnouncementsResource()

        now = self._now()
        self._refresh_if_needed(now)
        items = [
            item
            for item in (self._cached_items or [])
            if item.published_at <= now
            and (item.expires_at is None or item.expires_at > now)
        ]
        items.sort(key=lambda item: item.published_at, reverse=True)
        return StatusAnnouncementsResource(items=items)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _refresh_if_needed(self, now: datetime) -> None:
        if (
            self._cached_items is not None
            and self._cache_expires_at is not None
            and now < self._cache_expires_at
        ):
            return
        if self._next_attempt_at is not None and now < self._next_attempt_at:
            return

        if not self._lock.acquire(blocking=False):
            # 另一个请求已在刷新时直接读旧缓存，避免单账号场景下把请求线程
            # 卡在最长 10 秒的网络等待上。
            return
        try:
            if (
                self._cached_items is not None
                and self._cache_expires_at is not None
                and now < self._cache_expires_at
            ):
                return
            if self._next_attempt_at is not None and now < self._next_attempt_at:
                return
            self._refresh(now)
        finally:
            self._lock.release()

    def _refresh(self, now: datetime) -> None:
        feed_url = self.feed_url
        headers = {"Accept": "application/json"}
        if self._etag:
            headers["If-None-Match"] = self._etag

        try:
            with self._client_factory() as client:
                response = client.get(feed_url, headers=headers)
            if response.status_code == 304:
                if self._cached_items is not None:
                    self._cache_expires_at = now + self.CACHE_TTL
                    self._next_attempt_at = None
                    return
                raise ValueError("received 304 without a cached feed")
            response.raise_for_status()
            items = self._parse_feed(response.json())
        except (
            httpx.HTTPError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            self._next_attempt_at = now + self.RETRY_DELAY
            logger.warning(
                "Announcement feed refresh failed url={} error={}",
                feed_url,
                exc,
            )
            return

        self._cached_items = items
        self._cache_expires_at = now + self.CACHE_TTL
        self._next_attempt_at = None
        self._etag = response.headers.get("ETag")

    def _parse_feed(self, payload: Any) -> list[StatusAnnouncementResource]:
        if not isinstance(payload, dict):
            raise TypeError("announcement feed root must be an object")
        if payload.get("schema_version") != self.SUPPORTED_SCHEMA_VERSION:
            raise ValueError("unsupported announcement feed schema")
        raw_items = payload.get("announcements")
        if not isinstance(raw_items, list):
            raise TypeError("announcement feed items must be a list")

        items: list[StatusAnnouncementResource] = []
        seen_ids: set[str] = set()
        for raw_item in raw_items:
            try:
                item = StatusAnnouncementResource.model_validate(raw_item)
            except ValidationError as exc:
                logger.warning("Skipping invalid announcement entry: {}", exc)
                continue
            if item.id in seen_ids:
                logger.warning("Skipping duplicate announcement id={}", item.id)
                continue
            seen_ids.add(item.id)
            items.append(item)
        return items


announcement_service = AnnouncementService()
