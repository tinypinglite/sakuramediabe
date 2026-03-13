import json

import pytest
import toml

import src.config.config as config_module
from src.config.config import IndexerSettings, IndexerType
from src.model import DownloadClient, Indexer, MediaLibrary


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
    )

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def _create_client(name: str = "client-a") -> DownloadClient:
    library = MediaLibrary.create(name=f"library-{name}", root_path=f"/library/{name}")
    return DownloadClient.create(
        name=name,
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path=f"/downloads/{name}",
        local_root_path=f"/mnt/downloads/{name}",
        media_library=library,
    )


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
    download_client = _create_client()
    Indexer.create(
        name="initial",
        url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
        kind="bt",
        download_client=download_client,
    )
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
                "id": 1,
                "name": "initial",
                "url": "http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
                "kind": "bt",
                "download_client_id": download_client.id,
                "download_client_name": download_client.name,
            }
        ],
    }


def test_patch_indexer_settings_updates_and_subsequent_get_reads_new_values(
    client,
    account_user,
    isolated_indexer_settings,
):
    download_client = _create_client()
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
                    "download_client_id": download_client.id,
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
                "id": 1,
                "name": "mteam",
                "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                "kind": "pt",
                "download_client_id": download_client.id,
                "download_client_name": download_client.name,
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
    download_client = _create_client()
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/indexer-settings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "indexers": [
                {
                    "name": "mteam",
                    "url": "localhost:9117",
                    "kind": "pt",
                    "download_client_id": download_client.id,
                }
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_indexer_settings_url"


def test_patch_indexer_settings_rejects_unknown_download_client(
    client,
    account_user,
    isolated_indexer_settings,
):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/indexer-settings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "indexers": [
                {
                    "name": "mteam",
                    "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                    "kind": "pt",
                    "download_client_id": 999,
                }
            ]
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "indexer_settings_download_client_not_found"
