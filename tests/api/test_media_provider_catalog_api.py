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


def test_media_provider_catalog_localizes_display_name(
    client,
    account_user,
    monkeypatch,
):
    bundle = SimpleNamespace(
        provider_key="i18n",
        display_name="本地媒体库",
        display_name_i18n={"en": "Local library"},
        library_config_fields=(),
        playback_deliveries=("proxy",),
        downloads=None,
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "list_bundles", lambda: (bundle,))

    headers = _auth_headers(client, account_user)
    headers["Accept-Language"] = "en"
    response = client.get("/media-libraries/providers", headers=headers)

    assert response.status_code == 200
    assert response.json()[0]["display_name"] == "Local library"


def test_media_provider_catalog_localizes_config_fields(
    client,
    account_user,
    monkeypatch,
):
    bundle = SimpleNamespace(
        provider_key="i18n",
        display_name="I18n",
        library_config_fields=(
            ConfigField(
                key="token",
                label="令牌",
                input="secret",
                required=True,
                description="说明",
                hint="提示",
                label_i18n={"en": "Token"},
                description_i18n={"en": "Description"},
                hint_i18n={"en": "Hint"},
            ),
        ),
        playback_deliveries=("proxy",),
        downloads=None,
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "list_bundles", lambda: (bundle,))

    headers = _auth_headers(client, account_user)
    headers["Accept-Language"] = "en"
    response = client.get("/media-libraries/providers", headers=headers)

    assert response.status_code == 200
    field = response.json()[0]["library_config_fields"][0]
    assert field["label"] == "Token"
    assert field["description"] == "Description"
    assert field["hint"] == "Hint"
    assert "label_i18n" not in field
    assert "description_i18n" not in field
    assert "hint_i18n" not in field
