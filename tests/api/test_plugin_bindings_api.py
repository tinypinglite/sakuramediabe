"""统计/评论单插件绑定接口测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers.deps import db_deps, get_current_user
from src.api.routers.system.plugins import router
from src.config.config import settings
from src.plugins.extensions.stats import PluginMovieStatsField, PluginMovieStatsSource
from src.service.catalog.movie_review_source_service import MovieReviewSourceService
from src.service.catalog.movie_stats_source_service import MovieStatsSourceService


@pytest.fixture
def bindings_client(monkeypatch):
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    stats_source = PluginMovieStatsSource(
        fields=[
            PluginMovieStatsField(
                key="score",
                value_type="float",
                label_i18n={"zh-CN": "评分", "en": "Score"},
                max=5,
            )
        ],
        heat_version="test-v1",
        fetch_stats=lambda _number: None,
    )
    review_source = type(
        "ReviewSource",
        (),
        {"fetch_reviews": lambda *_args: None},
    )()
    monkeypatch.setattr(
        MovieStatsSourceService,
        "sources",
        (("stats_plugin", "Stats Plugin", {"en": "Stats Plugin"}, stats_source),),
    )
    monkeypatch.setattr(
        MovieReviewSourceService,
        "sources",
        (("review_plugin", "Review Plugin", {"en": "Review Plugin"}, review_source),),
    )
    monkeypatch.setattr(
        settings.plugins,
        "enabled",
        ["stats_plugin", "review_plugin"],
    )
    monkeypatch.setattr(settings.plugins, "stats_source", None)
    monkeypatch.setattr(settings.plugins, "review_source", None)
    monkeypatch.setattr(
        "src.service.system.plugin_binding_service.update_settings",
        lambda updated: setattr(settings, "plugins", updated.plugins),
    )

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[db_deps] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: object()
    with TestClient(app) as client:
        yield client


def test_get_bindings_lists_capable_sources_and_legacy_fields(bindings_client):
    response = bindings_client.get("/system/plugins/bindings")

    assert response.status_code == 200
    body = response.json()
    assert body["stats_source"] is None
    assert body["available_stats_sources"] == [
        {"plugin_id": "stats_plugin", "display_name": "Stats Plugin"}
    ]
    assert body["available_review_sources"] == [
        {"plugin_id": "review_plugin", "display_name": "Review Plugin"}
    ]
    assert {field["key"] for field in body["stats_fields"]} >= {
        "score",
        "score_number",
        "watched_count",
        "want_watch_count",
        "comment_count",
    }


def test_update_bindings_sets_single_stats_and_review_source(bindings_client):
    response = bindings_client.put(
        "/system/plugins/bindings",
        json={"stats_source": "stats_plugin", "review_source": "review_plugin"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stats_source"] == "stats_plugin"
    assert body["review_source"] == "review_plugin"
    assert body["stats_source_available"] is True
    assert body["review_source_available"] is True
    assert body["heat_version"] == "test-v1"
    assert body["pending_restart"] == ["api", "aps"]
    assert settings.plugins.stats_source == "stats_plugin"
    assert settings.plugins.review_source == "review_plugin"


def test_update_bindings_rejects_unavailable_plugin(bindings_client):
    response = bindings_client.put(
        "/system/plugins/bindings",
        json={"stats_source": "missing", "review_source": None},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_binding"
