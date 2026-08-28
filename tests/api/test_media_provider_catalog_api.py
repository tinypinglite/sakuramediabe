from types import SimpleNamespace

from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY, ConfigField


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_provider_catalog_is_sorted_and_exposes_field_metadata(
    client,
    account_user,
    monkeypatch,
):
    first = SimpleNamespace(
        provider_key="alpha",
        display_name="Alpha",
        library_config_fields=(
            ConfigField(
                key="cookie",
                label="Cookie",
                input="secret",
                required=True,
                description="登录凭据",
            ),
        ),
        playback_deliveries=("proxy",),
        downloads=None,
    )
    second = SimpleNamespace(
        provider_key="zeta",
        display_name="Zeta",
        library_config_fields=(),
        playback_deliveries=("proxy", "redirect"),
        downloads=SimpleNamespace(config_fields=()),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "list_bundles", lambda: (first, second))
    response = client.get(
        "/media-libraries/providers",
        headers=_auth_headers(client, account_user),
    )
    assert response.status_code == 200
    assert [item["provider_key"] for item in response.json()] == ["alpha", "zeta"]
    assert response.json()[0]["library_config_fields"][0]["input"] == "secret"
    assert response.json()[0]["library_config_fields"][0]["description"] == "登录凭据"
    assert response.json()[0]["playback_deliveries"] == ["proxy"]
    assert response.json()[0]["download_config_fields"] is None
    assert response.json()[1]["playback_deliveries"] == ["proxy", "redirect"]
    assert response.json()[1]["download_config_fields"] == []
