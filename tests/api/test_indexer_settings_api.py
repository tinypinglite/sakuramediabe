import json

import pytest
import toml

import src.config.config as config_module
from src.config.config import IndexerItem, IndexerKind, IndexerSettings, IndexerType


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


@pytest.fixture()
def isolated_indexer_settings(tmp_path, monkeypatch):
    original_runtime_settings = config_module.Settings.model_validate(
        config_module.settings.model_dump()
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(json.loads(original_runtime_settings.model_dump_json())),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    config_module.settings.indexer_settings = IndexerSettings(
        type=IndexerType.JACKETT,
        api_key="initial-key",
        indexers=[
            IndexerItem(
                name="initial",
                url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
                kind=IndexerKind.BT,
            )
        ],
    )

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def test_indexer_settings_endpoints_require_authentication(client):
    get_response = client.get("/indexer-settings")
    patch_response = client.patch("/indexer-settings", json={"api_key": "updated-key"})

    assert get_response.status_code == 401
    assert patch_response.status_code == 401


def test_get_indexer_settings_returns_current_configuration(
    client,
    account_user,
    isolated_indexer_settings,
):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/indexer-settings",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "type": "jackett",
        "api_key": "initial-key",
        "indexers": [
            {
                "name": "initial",
                "url": "http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
                "kind": "bt",
            }
        ],
    }


def test_patch_indexer_settings_updates_and_subsequent_get_reads_new_values(
    client,
    account_user,
    isolated_indexer_settings,
):
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    patch_response = client.patch(
        "/indexer-settings",
        headers=headers,
        json={
            "api_key": "updated-key",
            "indexers": [
                {
                    "name": "mteam",
                    "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                    "kind": "pt",
                }
            ],
        },
    )
    get_response = client.get("/indexer-settings", headers=headers)

    assert patch_response.status_code == 200
    assert patch_response.json() == {
        "type": "jackett",
        "api_key": "updated-key",
        "indexers": [
            {
                "name": "mteam",
                "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                "kind": "pt",
            }
        ],
    }
    assert get_response.status_code == 200
    assert get_response.json() == patch_response.json()


def test_patch_indexer_settings_returns_domain_error_payload(
    client,
    account_user,
    isolated_indexer_settings,
):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/indexer-settings",
        headers={"Authorization": f"Bearer {token}"},
        json={"indexers": [{"name": "mteam", "url": "localhost:9117", "kind": "pt"}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_indexer_settings_url"
