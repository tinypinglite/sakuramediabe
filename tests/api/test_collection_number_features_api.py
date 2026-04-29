import json

import pytest
import toml

import src.config.config as config_module
from src.config.config import Settings


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


@pytest.fixture()
def isolated_collection_number_features(tmp_path, monkeypatch):
    original_runtime_settings = Settings.model_validate(
        config_module.settings.model_dump()
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(json.loads(original_runtime_settings.model_dump_json())),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    config_module.settings.media.others_number_features = {"OFJE", "CJOB"}

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def _persisted_features(config_path) -> set[str]:
    return set(toml.load(config_path)["media"]["others_number_features"])


def test_collection_number_features_endpoints_require_authentication(client):
    get_response = client.get("/collection-number-features")
    patch_response = client.patch(
        "/collection-number-features",
        json={"features": ["OFJE"]},
    )

    assert get_response.status_code == 401
    assert patch_response.status_code == 401


def test_get_collection_number_features_returns_normalized_sorted_features(
    client,
    account_user,
    isolated_collection_number_features,
):
    config_module.settings.media.others_number_features = {" ofje ", "fc2"}
    token = _login(client, username=account_user.username)

    response = client.get(
        "/collection-number-features",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "features": ["FC2", "OFJE"],
        "sync_stats": None,
    }


def test_patch_collection_number_features_defaults_apply_now_true_and_returns_sync_stats(
    client,
    account_user,
    isolated_collection_number_features,
    monkeypatch: pytest.MonkeyPatch,
):
    expected_stats = {
        "total_movies": 4,
        "matched_count": 2,
        "updated_to_collection_count": 1,
        "updated_to_single_count": 1,
        "unchanged_count": 2,
    }
    sync_called = {"called": False}

    def _fake_sync():
        sync_called["called"] = True
        return expected_stats

    monkeypatch.setattr(
        "src.service.system.collection_number_features_service.MovieCollectionService.sync_movie_collections",
        _fake_sync,
    )
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/collection-number-features",
        headers={"Authorization": f"Bearer {token}"},
        json={"features": [" ofje ", "FC2", "fc2"]},
    )

    assert response.status_code == 200
    assert sync_called["called"] is True
    assert response.json() == {
        "features": ["FC2", "OFJE"],
        "sync_stats": expected_stats,
    }
    assert config_module.settings.media.others_number_features == {"FC2", "OFJE"}
    assert _persisted_features(isolated_collection_number_features) == {"FC2", "OFJE"}


def test_patch_collection_number_features_apply_now_false_skips_sync(
    client,
    account_user,
    isolated_collection_number_features,
    monkeypatch: pytest.MonkeyPatch,
):
    def _unexpected_sync_call():
        raise AssertionError("sync should not run when apply_now is false")

    monkeypatch.setattr(
        "src.service.system.collection_number_features_service.MovieCollectionService.sync_movie_collections",
        _unexpected_sync_call,
    )
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/collection-number-features?apply_now=false",
        headers={"Authorization": f"Bearer {token}"},
        json={"features": ["DVAJ", "ofje"]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "features": ["DVAJ", "OFJE"],
        "sync_stats": None,
    }
    assert config_module.settings.media.others_number_features == {"DVAJ", "OFJE"}
    assert _persisted_features(isolated_collection_number_features) == {"DVAJ", "OFJE"}


def test_patch_collection_number_features_rejects_invalid_feature(
    client,
    account_user,
    isolated_collection_number_features,
):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/collection-number-features",
        headers={"Authorization": f"Bearer {token}"},
        json={"features": ["   ", "OFJE"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_collection_number_feature"
