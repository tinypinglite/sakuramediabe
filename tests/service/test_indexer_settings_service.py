import json

import pytest
import toml

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.config.config import IndexerItem, IndexerKind, IndexerSettings, IndexerType
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


def test_get_settings_returns_current_indexer_configuration(isolated_indexer_settings):
    resource = IndexerSettingsService.get_settings()

    assert resource.model_dump() == {
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


def test_update_settings_merges_type_and_api_key(isolated_indexer_settings):
    resource = IndexerSettingsService.update_settings(
        IndexerSettingsUpdateRequest(type=" jackett ", api_key=" updated-key ")
    )

    assert resource.type is IndexerType.JACKETT
    assert resource.api_key == "updated-key"
    assert resource.indexers[0].name == "initial"


def test_update_settings_replaces_indexers_list(isolated_indexer_settings):
    resource = IndexerSettingsService.update_settings(
        IndexerSettingsUpdateRequest(
            indexers=[
                IndexerItemUpdatePayload(
                    name="mteam",
                    url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                    kind="pt",
                ),
                IndexerItemUpdatePayload(
                    name="dmhy",
                    url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                    kind="bt",
                ),
            ]
        )
    )

    assert resource.model_dump()["indexers"] == [
        {
            "name": "mteam",
            "url": "http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
            "kind": "pt",
        },
        {
            "name": "dmhy",
            "url": "https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
            "kind": "bt",
        },
    ]


def test_update_settings_rejects_empty_payload(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(IndexerSettingsUpdateRequest())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_indexer_settings_update"


def test_update_settings_rejects_empty_api_key(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(api_key="   ")
        )

    assert exc_info.value.code == "invalid_indexer_settings_api_key"


def test_update_settings_rejects_invalid_url(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="localhost:9117",
                        kind="pt",
                    )
                ]
            )
        )

    assert exc_info.value.code == "invalid_indexer_settings_url"


def test_update_settings_rejects_null_indexers(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest.model_validate({"indexers": None})
        )

    assert exc_info.value.code == "invalid_indexer_settings_indexers"


def test_update_settings_rejects_duplicate_names(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                        kind="pt",
                    ),
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                        kind="bt",
                    ),
                ]
            )
        )

    assert exc_info.value.code == "duplicate_indexer_settings_name"


def test_update_settings_rejects_invalid_kind(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
                        kind="rss",
                    )
                ]
            )
        )

    assert exc_info.value.code == "invalid_indexer_settings_kind"


def test_update_settings_rejects_unsupported_type(isolated_indexer_settings):
    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(type="prowlarr")
        )

    assert exc_info.value.code == "invalid_indexer_settings_type"
