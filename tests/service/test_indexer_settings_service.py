import json

import pytest
import toml

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.config.config import IndexerSettings, IndexerType
from src.model import DownloadClient, Indexer, IndexerDownloadClient, MediaLibrary
from src.schema.system.indexer_settings import (
    IndexerItemUpdatePayload,
    IndexerSettingsUpdateRequest,
)
from src.service.system.indexer_settings_service import IndexerSettingsService
from src.service.transfers.jackett_client import JackettClientError



def _create_indexer(*, download_client, **fields):
    """建索引器并写多对多绑定，替代旧的单 FK 直连写法。"""
    indexer = Indexer.create(**fields)
    IndexerDownloadClient.create(indexer=indexer, download_client=download_client)
    return indexer

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
    models = [MediaLibrary, DownloadClient, Indexer, IndexerDownloadClient]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


def _create_client(name: str = "client-a") -> DownloadClient:
    library = MediaLibrary.create(name=f"library-{name}", backend="local", backend_config={"root_path": f"/library/{name}"})
    return DownloadClient.create(
        name=name,
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path=f"/downloads/{name}",
        local_root_path=f"/mnt/downloads/{name}",
        media_library=library,
    )


def _create_cloud115_client(name: str = "cloud115-a") -> DownloadClient:
    library = MediaLibrary.create(
        name=f"library-{name}",
        backend="cloud115",
        backend_config={"cookies": "UID=test_A1_1", "root_cid": "root"},
    )
    return DownloadClient.create(name=name, kind="cloud115", media_library=library)


def test_get_settings_returns_current_indexer_configuration(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    _create_indexer(
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
                "download_clients": [
                    {"id": client.id, "name": client.name, "kind": "qbittorrent"}
                ],
            }
        ],
    }


def test_update_settings_merges_type_and_api_key(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    _create_indexer(
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
                    download_client_ids=[client_a.id],
                ),
                IndexerItemUpdatePayload(
                    name="dmhy",
                    url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                    kind="bt",
                    download_client_ids=[client_b.id],
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
            "download_clients": [
                {"id": client_a.id, "name": client_a.name, "kind": "qbittorrent"}
            ],
        },
        {
            "id": 2,
            "name": "dmhy",
            "url": "https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
            "kind": "bt",
            "download_clients": [
                {"id": client_b.id, "name": client_b.name, "kind": "qbittorrent"}
            ],
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
                        download_client_ids=[client.id],
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
                        download_client_ids=[client.id],
                    ),
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
                        kind="bt",
                        download_client_ids=[client.id],
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
                        download_client_ids=[client.id],
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
                        download_client_ids=[999],
                    )
                ]
            )
        )

    assert exc_info.value.code == "indexer_settings_download_client_not_found"


def test_update_settings_rejects_pt_binding_to_cloud115(
    isolated_indexer_settings, indexer_tables
):
    qb_client = _create_client()
    cloud_client = _create_cloud115_client()
    existing = _create_indexer(
        name="existing",
        url="https://example.com/existing",
        kind="bt",
        download_client=qb_client,
    )

    with pytest.raises(ApiError) as exc_info:
        IndexerSettingsService.update_settings(
            IndexerSettingsUpdateRequest(
                indexers=[
                    IndexerItemUpdatePayload(
                        name="mteam",
                        url="https://example.com/mteam",
                        kind="pt",
                        download_client_ids=[qb_client.id, cloud_client.id],
                    )
                ]
            )
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "pt_indexer_cloud115_binding_unsupported"
    assert exc_info.value.details == {
        "indexer_name": "mteam",
        "download_client_id": cloud_client.id,
    }
    # 校验发生在整体替换前，原配置不能被非法请求清空。
    assert [item.id for item in Indexer.select()] == [existing.id]
    assert IndexerDownloadClient.select().count() == 1


def test_update_settings_allows_bt_binding_to_cloud115(
    isolated_indexer_settings, indexer_tables
):
    cloud_client = _create_cloud115_client()

    resource = IndexerSettingsService.update_settings(
        IndexerSettingsUpdateRequest(
            indexers=[
                IndexerItemUpdatePayload(
                    name="dmhy",
                    url="https://example.com/dmhy",
                    kind="bt",
                    download_client_ids=[cloud_client.id],
                )
            ]
        )
    )

    assert resource.indexers[0].kind.value == "bt"
    assert resource.indexers[0].download_clients[0].kind == "cloud115"


def test_test_connection_reports_healthy_with_result_count(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    _create_indexer(
        name="initial",
        url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
        kind="bt",
        download_client=client,
    )

    class FakeJackettClient:
        def search(self, movie_number):
            assert movie_number == IndexerSettingsService.CONNECTION_TEST_QUERY
            return [object(), object()]

    result = IndexerSettingsService.test_connection(jackett_client_cls=FakeJackettClient)

    assert result.healthy is True
    assert result.query == "SSNI-888"
    assert result.indexers_checked == 1
    assert result.result_count == 2
    assert result.error is None


def test_test_connection_returns_unhealthy_on_jackett_error(isolated_indexer_settings, indexer_tables):
    client = _create_client()
    _create_indexer(
        name="initial",
        url="http://127.0.0.1:9117/api/v2.0/indexers/initial/results/torznab/",
        kind="bt",
        download_client=client,
    )

    class FakeJackettClient:
        def search(self, movie_number):
            raise JackettClientError("connection refused")

    result = IndexerSettingsService.test_connection(jackett_client_cls=FakeJackettClient)

    assert result.healthy is False
    assert result.indexers_checked == 1
    assert result.result_count == 0
    assert result.error is not None
    assert result.error.type == "jackett_request_error"
    assert result.error.message == "connection refused"


def test_test_connection_returns_unhealthy_when_no_indexers_configured(isolated_indexer_settings, indexer_tables):
    result = IndexerSettingsService.test_connection()

    assert result.healthy is False
    assert result.indexers_checked == 0
    assert result.error is not None
    assert result.error.type == "no_indexers_configured"
