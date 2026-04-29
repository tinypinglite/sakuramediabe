import json

import pytest
import toml

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.config.config import IndexerSettings, IndexerType
from src.model import DownloadClient, Indexer, MediaLibrary
from src.schema.system.indexer_settings import (
    IndexerItemUpdatePayload,
    IndexerSettingsUpdateRequest,
)
from src.service.system.indexer_settings_service import IndexerSettingsService


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


@pytest.fixture()
def indexer_tables(test_db):
    models = [MediaLibrary, DownloadClient, Indexer]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


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


def test_get_settings_returns_current_indexer_configuration(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    Indexer.create(
        name="initial",
        url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
        kind="bt",
        download_client=client,
    )
    resource = IndexerSettingsService.get_settings()

    assert resource.model_dump() == {
        "type": "jackett",
        "api_key": "initial-key",
        "indexers": [
            {
                "id": 1,
                "name": "initial",
                "url": "http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
                "kind": "bt",
                "download_client_id": client.id,
                "download_client_name": client.name,
            }
        ],
    }


def test_update_settings_merges_type_and_api_key(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    Indexer.create(
        name="initial",
        url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
        kind="bt",
        download_client=client,
    )
    resource = IndexerSettingsService.update_settings(
        IndexerSettingsUpdateRequest(type=" jackett ", api_key=" updated-key ")
    )

    assert resource.type is IndexerType.JACKETT
    assert resource.api_key == "updated-key"
    assert resource.indexers[0].name == "initial"


def test_update_settings_replaces_indexers_list(isolated_indexer_settings, indexer_tables):
    client_a = _create_client("client-a")
    client_b = _create_client("client-b")
    resource = IndexerSettingsService.update_settings(
        IndexerSettingsUpdateRequest(
            indexers=[
                IndexerItemUpdatePayload(
                    name="mteam",
                    url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                    kind="pt",
                    download_client_id=client_a.id,
                ),
                IndexerItemUpdatePayload(
                    name="dmhy",
                    url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                    kind="bt",
                    download_client_id=client_b.id,
                ),
            ]
        )
    )

    assert resource.model_dump()["indexers"] == [
        {
            "id": 1,
            "name": "mteam",
            "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
            "kind": "pt",
            "download_client_id": client_a.id,
            "download_client_name": client_a.name,
        },
        {
            "id": 2,
            "name": "dmhy",
            "url": "https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
            "kind": "bt",
            "download_client_id": client_b.id,
            "download_client_name": client_b.name,
        },
    ]


def test_update_settings_rejects_empty_payload(isolated_indexer_settings, indexer_tables):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(IndexerSettingsUpdateRequest())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_indexer_settings_update"


def test_update_settings_rejects_empty_api_key(isolated_indexer_settings, indexer_tables):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(api_key="   ")
        )

    assert exc_info.value.code == "invalid_indexer_settings_api_key"


def test_update_settings_rejects_invalid_url(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="localhost:9117",
                        kind="pt",
                        download_client_id=client.id,
                    )
                ]
            )
        )

    assert exc_info.value.code == "invalid_indexer_settings_url"


def test_update_settings_rejects_null_indexers(isolated_indexer_settings, indexer_tables):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest.model_validate({"indexers": None})
        )

    assert exc_info.value.code == "invalid_indexer_settings_indexers"


def test_update_settings_rejects_duplicate_names(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                        kind="pt",
                        download_client_id=client.id,
                    ),
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                        kind="bt",
                        download_client_id=client.id,
                    ),
                ]
            )
        )

    assert exc_info.value.code == "duplicate_indexer_settings_name"


def test_update_settings_rejects_invalid_kind(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                        kind="rss",
                        download_client_id=client.id,
                    )
                ]
            )
        )

    assert exc_info.value.code == "invalid_indexer_settings_kind"


def test_update_settings_rejects_unsupported_type(isolated_indexer_settings, indexer_tables):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(type="prowlarr")
        )

    assert exc_info.value.code == "invalid_indexer_settings_type"


def test_update_settings_rejects_unknown_download_client(isolated_indexer_settings, indexer_tables):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                        kind="pt",
                        download_client_id=999,
                    )
                ]
            )
        )

    assert exc_info.value.code == "indexer_settings_download_client_not_found"
