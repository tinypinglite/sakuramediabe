from __future__ import annotations

import base64
import json
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from src.config.config import settings
from src.metadata._providers.exceptions import (
    JavdbAuthError,
    JavdbAuthRequiredError,
    MetadataRequestError,
)
from src.metadata._providers.javdb import JavdbProvider
from src.metadata._providers.javdb_auth import (
    JavdbAuthManager,
    javdb_account_fingerprint,
)
from src.metadata.factory import build_javdb_provider


def _jwt_token(expires_at: float) -> str:
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": int(expires_at)}).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"header.{payload}.signature"


def _manager(path, **kwargs) -> JavdbAuthManager:
    return JavdbAuthManager(path, process_role="test", **kwargs)


@pytest.fixture
def session_path(tmp_path):
    return tmp_path / "javdb-session.json"


@pytest.fixture
def account_fingerprint() -> str:
    return javdb_account_fingerprint("example.com", "user", "password")


def _search_success(movie_number: str = "ABP-001") -> dict:
    return {
        "success": 1,
        "data": {
            "movies": [
                {
                    "id": "movie-id",
                    "number": movie_number,
                    "title": "title",
                    "release_date": "2026-01-01",
                }
            ]
        },
    }


def _auth_required() -> dict:
    return {
        "success": 0,
        "action": "JWTVerificationError",
        "message": "請登錄帳號",
        "data": None,
    }


def test_two_managers_share_file_token(
    session_path,
    account_fingerprint,
) -> None:
    first = _manager(session_path)
    second = _manager(session_path)
    token = _jwt_token(time.time() + 3600)
    first_login = Mock(return_value=token)
    second_login = Mock(return_value="other-token")

    assert (
        first.get_token(first_login, account_fingerprint=account_fingerprint)
        == token
    )
    assert (
        second.get_token(second_login, account_fingerprint=account_fingerprint)
        == token
    )

    first_login.assert_called_once()
    second_login.assert_not_called()
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600


def test_concurrent_refresh_performs_single_login(
    session_path,
    account_fingerprint,
) -> None:
    token = _jwt_token(time.time() + 3600)
    login_count = 0
    count_lock = threading.Lock()

    def login() -> str:
        nonlocal login_count
        with count_lock:
            login_count += 1
        time.sleep(0.05)
        return token

    managers = [_manager(session_path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda manager: manager.get_token(
                    login, account_fingerprint=account_fingerprint
                ),
                managers * 2,
            )
        )

    assert results == [token] * 8
    assert login_count == 1


def test_expired_token_refreshes(
    session_path,
    account_fingerprint,
) -> None:
    now = 1_000_000.0
    manager = _manager(session_path, clock=lambda: now)
    first_token = _jwt_token(now + 10)
    second_token = _jwt_token(now + 3600)
    login = Mock(side_effect=[first_token, second_token])

    assert (
        manager.get_token(login, account_fingerprint=account_fingerprint)
        == first_token
    )
    assert (
        manager.get_token(login, account_fingerprint=account_fingerprint)
        == second_token
    )
    assert login.call_count == 2


def test_conditional_invalidate_keeps_newer_token(
    session_path,
    account_fingerprint,
) -> None:
    first = _manager(session_path)
    second = _manager(session_path)
    old_token = _jwt_token(time.time() + 3600)
    new_token = _jwt_token(time.time() + 7200)
    first_login = Mock(return_value=old_token)
    second_login = Mock(return_value=new_token)

    assert (
        first.get_token(first_login, account_fingerprint=account_fingerprint)
        == old_token
    )
    second.invalidate("stale-token", account_fingerprint=account_fingerprint)
    assert (
        first.get_token(second_login, account_fingerprint=account_fingerprint)
        == old_token
    )
    second_login.assert_not_called()

    first.invalidate(old_token, account_fingerprint=account_fingerprint)
    assert (
        second.get_token(second_login, account_fingerprint=account_fingerprint)
        == new_token
    )


def test_auth_failure_cooldown_is_shared(
    session_path,
    account_fingerprint,
) -> None:
    clock = [1_000_000.0]
    first = _manager(session_path, clock=lambda: clock[0])
    second = _manager(session_path, clock=lambda: clock[0])
    login = Mock(side_effect=JavdbAuthError("bad credentials"))

    with pytest.raises(JavdbAuthError, match="bad credentials"):
        first.get_token(login, account_fingerprint=account_fingerprint)
    with pytest.raises(JavdbAuthError, match="temporarily skipped"):
        second.get_token(login, account_fingerprint=account_fingerprint)
    assert login.call_count == 1

    clock[0] += 31
    recovery_login = Mock(return_value=_jwt_token(clock[0] + 3600))
    assert (
        second.get_token(
            recovery_login, account_fingerprint=account_fingerprint
        )
        == recovery_login.return_value
    )


def test_network_failure_does_not_poison_shared_state(
    session_path,
    account_fingerprint,
) -> None:
    manager = _manager(session_path)
    login = Mock(
        side_effect=MetadataRequestError(
            "POST", "https://example.com", "timeout"
        )
    )

    with pytest.raises(MetadataRequestError, match="timeout"):
        manager.get_token(login, account_fingerprint=account_fingerprint)

    recovery_login = Mock(return_value=_jwt_token(time.time() + 3600))
    assert (
        manager.get_token(
            recovery_login, account_fingerprint=account_fingerprint
        )
        == recovery_login.return_value
    )


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        json.dumps({"version": 999, "token": "token"}),
    ],
)
def test_invalid_shared_state_is_replaced(
    session_path,
    account_fingerprint,
    payload,
) -> None:
    session_path.write_text(payload, encoding="utf-8")
    manager = _manager(session_path)
    token = _jwt_token(time.time() + 3600)
    login = Mock(return_value=token)

    assert (
        manager.get_token(login, account_fingerprint=account_fingerprint)
        == token
    )
    assert json.loads(session_path.read_text(encoding="utf-8"))["token"] == token


def test_protected_search_requires_login_when_unconfigured(
    session_path,
) -> None:
    provider = JavdbProvider(
        "auth-required.example",
        auth_manager=_manager(session_path),
    )

    with pytest.raises(JavdbAuthRequiredError, match="not configured"):
        provider._search_movie("ABP-001")


def test_device_payload_has_no_vendor_fingerprint(session_path) -> None:
    provider = JavdbProvider(
        "device.example",
        username="user",
        password="password",
        auth_manager=_manager(session_path),
    )

    payload = provider._device_payload()

    assert payload["device_name"] == "Android"
    assert payload["device_model"] == "Android"
    assert not any("meizu" in value.lower() for value in payload.values())


def test_search_refreshes_token_once_after_jwt_error(
    session_path,
    monkeypatch,
) -> None:
    provider = JavdbProvider(
        "refresh.example",
        username="user-refresh",
        password="password",
        auth_manager=_manager(session_path),
    )
    login = Mock(return_value="token")
    monkeypatch.setattr(provider, "_login", login)
    request = Mock(side_effect=[_auth_required(), _search_success()])
    monkeypatch.setattr(provider, "request_json", request)

    movie = provider._search_movie("ABP-001")

    assert movie["id"] == "movie-id"
    assert login.call_count == 2
    assert request.call_count == 2
    assert request.call_args_list[0].kwargs["headers"]["authorization"] == (
        "Bearer token"
    )
    assert request.call_args_list[1].kwargs["headers"]["authorization"] == (
        "Bearer token"
    )


def test_search_refreshes_token_once_after_http_auth_error(
    session_path,
    monkeypatch,
) -> None:
    provider = JavdbProvider(
        "refresh.example",
        username="user-refresh",
        password="password",
        auth_manager=_manager(session_path),
    )
    login = Mock(side_effect=["token-1", "token-2"])
    monkeypatch.setattr(provider, "_login", login)
    request = Mock(
        side_effect=[
            MetadataRequestError(
                "GET",
                "https://example.com",
                "unauthorized",
                status_code=401,
            ),
            _search_success(),
        ]
    )
    monkeypatch.setattr(provider, "request_json", request)

    assert provider._search_movie("ABP-001")["id"] == "movie-id"
    assert login.call_count == 2
    assert request.call_count == 2


def test_second_auth_failure_does_not_loop(
    session_path,
    monkeypatch,
) -> None:
    provider = JavdbProvider(
        "refresh.example",
        username="user-refresh",
        password="password",
        auth_manager=_manager(session_path),
    )
    login = Mock(side_effect=["token-1", "token-2"])
    monkeypatch.setattr(provider, "_login", login)
    request = Mock(return_value=_auth_required())
    monkeypatch.setattr(provider, "request_json", request)

    with pytest.raises(JavdbAuthRequiredError, match="請登錄帳號"):
        provider._search_movie("ABP-001")

    assert login.call_count == 2
    assert request.call_count == 2


def test_providers_share_token_through_manager(
    session_path,
    monkeypatch,
) -> None:
    first = JavdbProvider(
        "shared.example",
        username="user-shared",
        password="pw",
        auth_manager=_manager(session_path),
    )
    second = JavdbProvider(
        "shared.example",
        username="user-shared",
        password="pw",
        auth_manager=_manager(session_path),
    )
    login = Mock(return_value=_jwt_token(time.time() + 3600))
    monkeypatch.setattr(first, "_login", login)
    monkeypatch.setattr(second, "_login", login)
    monkeypatch.setattr(
        first, "request_json", Mock(return_value=_search_success())
    )
    monkeypatch.setattr(
        second, "request_json", Mock(return_value=_search_success())
    )

    assert first._search_movie("ABP-001")["id"] == "movie-id"
    assert second._search_movie("ABP-001")["id"] == "movie-id"
    assert login.call_count == 1


def test_login_failure_is_not_reported_as_missing_payload(
    session_path,
    monkeypatch,
) -> None:
    provider = JavdbProvider(
        "bad-login.example",
        username="u",
        password="p",
        auth_manager=_manager(session_path),
    )
    monkeypatch.setattr(
        provider,
        "_login",
        Mock(side_effect=JavdbAuthError("bad credentials")),
    )

    with pytest.raises(JavdbAuthError, match="bad credentials"):
        provider._search_movie("ABP-001")


def test_auth_required_payload_does_not_crash_on_null_data(
    session_path,
    monkeypatch,
) -> None:
    provider = JavdbProvider(
        "null-data.example",
        username="u",
        password="p",
        auth_manager=_manager(session_path),
    )
    monkeypatch.setattr(provider, "_ensure_logged_in", lambda: "token")
    monkeypatch.setattr(
        provider, "request_json", Mock(return_value=_auth_required())
    )

    with pytest.raises(JavdbAuthRequiredError, match="請登錄帳號"):
        provider._search_movie("ABP-001")


def test_javdb_provider_uses_unified_timeouts(session_path) -> None:
    provider = JavdbProvider(
        "timeout.example",
        username="u",
        password="p",
        auth_manager=_manager(session_path),
    )

    assert provider.timeout == 60.0
    assert provider.connect_timeout == 10.0
    assert provider.max_retries == 1
    assert provider.client.timeout.connect == 10.0
    assert provider.client.timeout.read == 60.0


def test_factory_uses_global_account_and_ignores_explicit_account(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings.metadata, "javdb_username", "global-user")
    monkeypatch.setattr(settings.metadata, "javdb_password", "global-password")

    global_provider = build_javdb_provider()
    plugin_provider = build_javdb_provider(
        username="plugin-user", password="plugin-password"
    )

    assert global_provider.provider.username == "global-user"
    assert global_provider.provider.password == "global-password"
    assert plugin_provider.provider.username == "global-user"
    assert plugin_provider.provider.password == "global-password"
