from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from src.api.exception.errors import ApiError
from src.plugins.provider_protocol import (
    PreparedLibraryLogin,
    ProviderOperationError,
    WebLoginCookieCapture,
    WebLoginLocalStorageCapture,
)
from src.schema.playback.media_libraries import (
    MediaLibraryProviderLoginSessionCreateRequest,
    MediaLibraryProviderLoginSessionResource,
)
from src.service.playback.provider_helpers import require_media_provider

LOGIN_SESSION_TTL_SECONDS = 600
MAX_CAPTURE_BYTES = 256 * 1024
MAX_CAPTURE_SETS = 32
MAX_CAPTURE_ITEMS = 512


@dataclass(frozen=True)
class _LoginSession:
    id: str
    provider_key: str
    provider_config: dict[str, Any]
    account_label: str | None
    expires_at: datetime
    expires_monotonic: float


class MediaLibraryLoginService:
    _sessions: dict[str, _LoginSession] = {}
    _lock = threading.Lock()

    @classmethod
    def create_session(
        cls,
        payload: MediaLibraryProviderLoginSessionCreateRequest,
    ) -> MediaLibraryProviderLoginSessionResource:
        provider_key = payload.provider_key.strip()
        if not provider_key:
            raise ApiError(
                422,
                "invalid_media_library_provider",
                "媒体库 Provider 无效",
            )
        bundle = require_media_provider(provider_key)

        login_spec = getattr(bundle, "library_login", None)
        resolver = getattr(bundle, "resolve_library_login", None)
        if login_spec is None or not callable(resolver):
            raise ApiError(
                422,
                "provider_login_unsupported",
                "该媒体库提供方不支持网页登录",
            )

        captured = cls._capture_payload(payload, login_spec=login_spec)
        try:
            prepared = resolver(captured=captured)
        except ProviderOperationError as exc:
            status_code = 503 if exc.code == "unavailable" else 422
            raise ApiError(status_code, "provider_login_failed", exc.safe_message) from exc
        if not isinstance(prepared, PreparedLibraryLogin):
            raise ApiError(
                502,
                "provider_invalid_response",
                "媒体提供方返回了无效响应",
            )
        provider_config = dict(prepared.provider_config)
        account_label = (
            prepared.account_label.strip()
            if isinstance(prepared.account_label, str)
            else None
        )
        if account_label and len(account_label) > 160:
            account_label = account_label[:160]
        now = datetime.now().astimezone()
        session = _LoginSession(
            id=secrets.token_urlsafe(32),
            provider_key=provider_key,
            provider_config=provider_config,
            account_label=account_label,
            expires_at=now + timedelta(seconds=LOGIN_SESSION_TTL_SECONDS),
            expires_monotonic=time.monotonic() + LOGIN_SESSION_TTL_SECONDS,
        )
        with cls._lock:
            cls._purge_expired_locked()
            cls._sessions[session.id] = session
        logger.info(
            "Media provider login session created provider_key={} account_key={}",
            provider_key,
            prepared.account_key,
        )
        return MediaLibraryProviderLoginSessionResource(
            id=session.id,
            provider_key=session.provider_key,
            account_label=session.account_label,
            expires_at=session.expires_at,
        )

    @classmethod
    def delete_session(cls, session_id: str) -> None:
        with cls._lock:
            cls._sessions.pop(session_id, None)

    @classmethod
    def get_session(
        cls,
        session_id: str,
        *,
        provider_key: str,
    ) -> _LoginSession:
        with cls._lock:
            cls._purge_expired_locked()
            session = cls._sessions.get(session_id)
            if session is None:
                raise ApiError(
                    404,
                    "media_library_provider_login_session_not_found",
                    "登录会话不存在或已过期，请重新登录",
                )
            if session.provider_key != provider_key:
                raise ApiError(
                    409,
                    "media_library_provider_login_session_provider_mismatch",
                    "登录会话与媒体库提供方不匹配",
                )
            return replace(session, provider_config=dict(session.provider_config))

    @classmethod
    def _purge_expired_locked(cls) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in cls._sessions.items()
            if session.expires_monotonic <= now
        ]
        for session_id in expired:
            cls._sessions.pop(session_id, None)

    @classmethod
    def _capture_payload(cls, payload, *, login_spec) -> dict[str, Any]:
        if len(payload.cookies) > MAX_CAPTURE_SETS:
            cls._invalid_capture()
        if len(payload.local_storage) > MAX_CAPTURE_SETS:
            cls._invalid_capture()

        cookie_sets = {item.url: item.values for item in payload.cookies}
        if len(cookie_sets) != len(payload.cookies):
            cls._invalid_capture()
        storage_sets = {item.origin.rstrip("/"): item.items for item in payload.local_storage}
        if len(storage_sets) != len(payload.local_storage):
            cls._invalid_capture()
        declared_cookie_urls = {
            capture.url for capture in login_spec.cookie_captures
        }
        if set(cookie_sets) - declared_cookie_urls:
            cls._invalid_capture()
        declared_storage_origins = {
            capture.origin.rstrip("/")
            for capture in login_spec.local_storage_captures
        }
        if set(storage_sets) - declared_storage_origins:
            cls._invalid_capture()

        captured_cookies: list[dict[str, Any]] = []
        for capture in login_spec.cookie_captures:
            if not isinstance(capture, WebLoginCookieCapture):
                cls._invalid_capture()
            values = cookie_sets.get(capture.url)
            if values is None:
                cls._invalid_capture()
            cls._validate_cookie_values(values, capture)
            captured_cookies.append(
                {
                    "url": capture.url,
                    "values": {
                        name: values[name]
                        for name in capture.capture_names
                        if name in values
                    },
                }
            )

        captured_storage: list[dict[str, Any]] = []
        for capture in login_spec.local_storage_captures:
            if not isinstance(capture, WebLoginLocalStorageCapture):
                cls._invalid_capture()
            origin = capture.origin.rstrip("/")
            items = storage_sets.get(origin)
            if items is None:
                cls._invalid_capture()
            cls._validate_storage_items(items, capture)
            captured_storage.append({"origin": origin, "items": dict(items)})

        captured: dict[str, Any] = {
            "cookies": captured_cookies,
            "local_storage": captured_storage,
        }
        try:
            encoded = json.dumps(
                captured,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422,
                "invalid_provider_login_capture",
                "网页登录信息不完整或无效",
            ) from exc
        if len(encoded) > MAX_CAPTURE_BYTES:
            cls._invalid_capture()
        return captured

    @staticmethod
    def _validate_cookie_values(
        values: dict[str, str],
        capture: WebLoginCookieCapture,
    ) -> None:
        if len(values) > MAX_CAPTURE_ITEMS:
            MediaLibraryLoginService._invalid_capture()
        allowed = set(capture.capture_names)
        if any(not name or name not in allowed for name in values):
            MediaLibraryLoginService._invalid_capture()
        if any(not isinstance(value, str) or not value for value in values.values()):
            MediaLibraryLoginService._invalid_capture()
        if any(name not in values for name in capture.required_names):
            MediaLibraryLoginService._invalid_capture()

    @staticmethod
    def _validate_storage_items(
        items: dict[str, str],
        capture: WebLoginLocalStorageCapture,
    ) -> None:
        if len(items) > MAX_CAPTURE_ITEMS:
            MediaLibraryLoginService._invalid_capture()
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            for key, value in items.items()
        ):
            MediaLibraryLoginService._invalid_capture()
        found: set[str] = set()
        for value in items.values():
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict):
                found.update(
                    key
                    for key in capture.required_json_keys
                    if isinstance(decoded.get(key), str) and decoded[key]
                )
        if any(key not in found for key in capture.required_json_keys):
            MediaLibraryLoginService._invalid_capture()

    @staticmethod
    def _invalid_capture() -> None:
        raise ApiError(
            422,
            "invalid_provider_login_capture",
            "网页登录信息不完整或无效",
        )


__all__ = [
    "LOGIN_SESSION_TTL_SECONDS",
    "MediaLibraryLoginService",
]
