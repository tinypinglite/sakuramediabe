from types import SimpleNamespace

from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ConfigField,
    PreparedLibrary,
)


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_library_rejects_explicit_read_only_config(client, account_user, monkeypatch):
    bundle = SimpleNamespace(
        provider_key="demo",
        library_config_fields=(
            ConfigField(key="account", label="Account", input="text", required=False, read_only=True),
        ),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: bundle)

    response = client.post(
        "/media-libraries",
        json={"name": "read-only", "provider_key": "demo", "provider_config": {"account": "user"}},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_library_provider_config"


def test_media_library_allows_multiple_libraries_for_one_provider_account(
    client, account_user, monkeypatch
):
    bundle = SimpleNamespace(
        provider_key="demo",
        library_config_fields=(),
        prepare_library=lambda **_kwargs: PreparedLibrary(
            provider_config={}, account_key="same-account"
        ),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "require", lambda _key: bundle)
    headers = _auth_headers(client, account_user)

    first = client.post(
        "/media-libraries",
        json={"name": "first", "provider_key": "demo", "provider_config": {}},
        headers=headers,
    )
    second = client.post(
        "/media-libraries",
        json={"name": "second", "provider_key": "demo", "provider_config": {}},
        headers=headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["account_key"] == second.json()["account_key"] == "same-account"
