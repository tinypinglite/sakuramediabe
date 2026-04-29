import json

import pytest
import toml

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.schema.system.collection_number_features import (
    CollectionNumberFeaturesUpdateRequest,
)
from src.service.system.collection_number_features_service import (
    CollectionNumberFeaturesService,
)


@pytest.fixture()
def isolated_collection_number_features(tmp_path, monkeypatch):
    original_runtime_settings = config_module.Settings.model_validate(
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


def test_get_features_returns_normalized_sorted_values(
    isolated_collection_number_features,
):
    config_module.settings.media.others_number_features = {" ofje ", "fc2", "  "}

    response = CollectionNumberFeaturesService.get_features()

    assert response.model_dump() == {
        "features": ["FC2", "OFJE"],
        "sync_stats": None,
    }


def test_update_features_deduplicates_and_persists_without_sync_when_apply_now_false(
    isolated_collection_number_features,
    monkeypatch: pytest.MonkeyPatch,
):
    def _unexpected_sync_call():
        raise AssertionError("sync should not run when apply_now is false")

    monkeypatch.setattr(
        "src.service.system.collection_number_features_service.MovieCollectionService.sync_movie_collections",
        _unexpected_sync_call,
    )

    response = CollectionNumberFeaturesService.update_features(
        CollectionNumberFeaturesUpdateRequest(features=[" ofje ", "FC2", "fc2"]),
        apply_now=False,
    )

    assert response.model_dump() == {
        "features": ["FC2", "OFJE"],
        "sync_stats": None,
    }
    assert config_module.settings.media.others_number_features == {"FC2", "OFJE"}
    assert _persisted_features(isolated_collection_number_features) == {"FC2", "OFJE"}


def test_update_features_allows_empty_list_to_clear_features(
    isolated_collection_number_features,
):
    response = CollectionNumberFeaturesService.update_features(
        CollectionNumberFeaturesUpdateRequest(features=[]),
        apply_now=False,
    )

    assert response.model_dump() == {
        "features": [],
        "sync_stats": None,
    }
    assert config_module.settings.media.others_number_features == set()
    assert _persisted_features(isolated_collection_number_features) == set()


def test_update_features_rejects_empty_patch_payload(
    isolated_collection_number_features,
):
    with pytest.raises(ApiError) as exc_info:
        CollectionNumberFeaturesService.update_features(
            CollectionNumberFeaturesUpdateRequest(),
            apply_now=False,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_collection_number_features_update"


def test_update_features_apply_now_runs_sync_and_returns_stats(
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

    response = CollectionNumberFeaturesService.update_features(
        CollectionNumberFeaturesUpdateRequest(features=["ofje", "DVAJ"]),
        apply_now=True,
    )

    assert sync_called["called"] is True
    assert response.model_dump() == {
        "features": ["DVAJ", "OFJE"],
        "sync_stats": expected_stats,
    }
    assert config_module.settings.media.others_number_features == {"DVAJ", "OFJE"}
    assert _persisted_features(isolated_collection_number_features) == {"DVAJ", "OFJE"}


def test_update_features_rolls_back_runtime_when_sync_fails(
    isolated_collection_number_features,
    monkeypatch: pytest.MonkeyPatch,
):
    initial_runtime_features = set(config_module.settings.media.others_number_features)
    initial_persisted_features = _persisted_features(isolated_collection_number_features)

    def _raise_sync_error():
        raise RuntimeError("sync failed")

    monkeypatch.setattr(
        "src.service.system.collection_number_features_service.MovieCollectionService.sync_movie_collections",
        _raise_sync_error,
    )

    with pytest.raises(RuntimeError):
        CollectionNumberFeaturesService.update_features(
            CollectionNumberFeaturesUpdateRequest(features=["FC2"]),
            apply_now=True,
        )

    assert config_module.settings.media.others_number_features == initial_runtime_features
    assert _persisted_features(isolated_collection_number_features) == initial_persisted_features


def test_update_features_rejects_invalid_feature(
    isolated_collection_number_features,
):
    with pytest.raises(ApiError) as exc_info:
        CollectionNumberFeaturesService.update_features(
            CollectionNumberFeaturesUpdateRequest(features=["   ", "OFJE"]),
            apply_now=False,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_collection_number_feature"
