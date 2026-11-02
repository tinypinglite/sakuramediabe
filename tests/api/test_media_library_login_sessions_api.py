from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import MediaLibrary
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ConfigField,
    LibraryLoginSpec,
    PreparedLibrary,
    PreparedLibraryLogin,
    WebLoginCookieCapture,
)
from src.service.playback import media_library_login_service
from src.service.playback.media_library_login_service import (
    MediaLibraryLoginService,
)


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _bundle(provider_key: str = "demo"):
    def prepare_library(*, submitted_config, previous):
        return PreparedLibrary(
            provider_config=dict(submitted_config),
            account_key="account",
        )

    return SimpleNamespace(
        provider_key=provider_key,
        display_name=provider_key.title(),
        library_config_fields=(
            ConfigField(
                key="session",
                label="Session",
                input="secret",
                required=True,
            ),
            ConfigField(
                key="path",
                label="Path",
                input="path",
                required=True,
            ),
            ConfigField(
                key="account_uid",
                label="Account",
                input="text",
                required=False,
                read_only=True,
            ),
        ),
        playback_deliveries=("proxy",),
        downloads=None,
        library_login=LibraryLoginSpec(
            start_url="https://example.com/login",
            allowed_hosts=("example.com",),
            cookie_captures=(
                WebLoginCookieCapture(
                    url="https://example.com/",
                    capture_names=("SESSION",),
                    required_names=("SESSION",),
                ),
            ),
            managed_config_keys=("session",),
        ),
        prepare_library=prepare_library,
        resolve_library_login=lambda *, captured: PreparedLibraryLogin(
            provider_config={
                "session": captured["cookies"][0]["values"]["SESSION"],
                "account_uid": "account",
            },
            account_key="account",
            account_label="account",
        ),
    )


@pytest.fixture(autouse=True)
def _clear_login_sessions():
    MediaLibraryLoginService._sessions.clear()
    yield
    MediaLibraryLoginService._sessions.clear()


def test_provider_catalog_exposes_library_login(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "list_bundles", lambda: (_bundle(),))

    response = client.get(
        "/media-libraries/providers",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    login = response.json()[0]["library_login"]
    assert login["mode"] == "webview"
    assert login["start_url"] == "https://example.com/login"
    assert login["allowed_hosts"] == ["example.com"]
    assert login["cookie_captures"] == [
        {
            "url": "https://example.com/",
            "capture_names": ["SESSION"],
            "required_names": ["SESSION"],
        }
    ]
    assert login["managed_config_keys"] == ["session"]


def test_provider_login_session_create_and_delete(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())
    headers = _auth_headers(client, account_user)

    response = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [
                {"url": "https://example.com/", "values": {"SESSION": "secret"}}
            ],
        },
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider_key"] == "demo"
    assert payload["account_label"] == "account"
    assert payload["id"]
    assert "secret" not in response.text

    deleted = client.delete(
        f"/media-libraries/provider-login-sessions/{payload['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204


def test_provider_login_session_rejects_incomplete_capture(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())

    response = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [{"url": "https://example.com/", "values": {}}],
        },
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_provider_login_capture"


def test_provider_login_session_rejects_provider_mismatch(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())
    session = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [
                {"url": "https://example.com/", "values": {"SESSION": "secret"}}
            ],
        },
        headers=_auth_headers(client, account_user),
    ).json()

    with pytest.raises(ApiError) as error:
        MediaLibraryLoginService.get_session(
            session["id"],
            provider_key="other",
        )

    assert (
        error.value.code
        == "media_library_provider_login_session_provider_mismatch"
    )


def test_create_library_consumes_login_session_and_rejects_managed_override(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())
    headers = _auth_headers(client, account_user)
    session = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [
                {"url": "https://example.com/", "values": {"SESSION": "from-login"}}
            ],
        },
        headers=headers,
    ).json()

    conflict = client.post(
        "/media-libraries",
        json={
            "name": "conflict",
            "provider_key": "demo",
            "provider_config": {"session": "manual", "path": "/media"},
            "auth_session_id": session["id"],
        },
        headers=headers,
    )
    assert conflict.status_code == 422
    assert (
        conflict.json()["error"]["code"]
        == "media_library_login_managed_config_conflict"
    )

    created = client.post(
        "/media-libraries",
        json={
            "name": "created-with-login",
            "provider_key": "demo",
            "provider_config": {"path": "/media"},
            "auth_session_id": session["id"],
        },
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["provider_config"]["path"] == "/media"
    stored = MediaLibrary.get(MediaLibrary.id == created.json()["id"])
    assert stored.provider_config["session"] == "from-login"

    reused = client.post(
        "/media-libraries",
        json={
            "name": "reused-session",
            "provider_key": "demo",
            "provider_config": {"path": "/media"},
            "auth_session_id": session["id"],
        },
        headers=headers,
    )
    assert reused.status_code == 404
    assert (
        reused.json()["error"]["code"]
        == "media_library_provider_login_session_not_found"
    )


def test_expired_provider_login_session_is_rejected(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())
    monkeypatch.setattr(media_library_login_service, "LOGIN_SESSION_TTL_SECONDS", -1)

    session = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [
                {"url": "https://example.com/", "values": {"SESSION": "secret"}}
            ],
        },
        headers=_auth_headers(client, account_user),
    ).json()

    response = client.post(
        "/media-libraries",
        json={
            "name": "expired-session",
            "provider_key": "demo",
            "provider_config": {"path": "/media"},
            "auth_session_id": session["id"],
        },
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 404
    assert (
        response.json()["error"]["code"]
        == "media_library_provider_login_session_not_found"
    )


def test_update_library_replaces_managed_credentials_with_login_session(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: _bundle())
    library = MediaLibrary.create(
        name="existing",
        provider_key="demo",
        provider_config={"session": "old", "path": "/old"},
        account_key="account",
    )
    headers = _auth_headers(client, account_user)
    session = client.post(
        "/media-libraries/provider-login-sessions",
        json={
            "provider_key": "demo",
            "cookies": [
                {"url": "https://example.com/", "values": {"SESSION": "new"}}
            ],
        },
        headers=headers,
    ).json()

    response = client.patch(
        f"/media-libraries/{library.id}",
        json={
            "provider_config": {"path": "/new"},
            "auth_session_id": session["id"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    stored = MediaLibrary.get(MediaLibrary.id == library.id)
    assert stored.provider_config["session"] == "new"
    assert stored.provider_config["path"] == "/new"
    with pytest.raises(ApiError) as missing:
        MediaLibraryLoginService.get_session(session["id"], provider_key="demo")
    assert (
        missing.value.code
        == "media_library_provider_login_session_not_found"
    )
