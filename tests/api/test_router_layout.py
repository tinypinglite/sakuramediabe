import logging
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers import deps
from src.api.routers.catalog import subscriptions as movie_subscriptions
from src.api.routers.catalog import tags
from src.api.routers.discovery import image_search, ranking_sources
from src.api.routers.discovery.hot_actress_releases import (
    router as hot_actress_releases_router,
)
from src.api.routers.playback import media_libraries
from src.api.routers.system import (
    account,
    activity,
    auth,
    indexer_settings,
    plugins,
    status,
)
from src.api.routers.system import config as system_config
from src.api.routers.transfers import downloads, media_import
from src.api.routers.videos import collections as video_collections
from src.api.routers.videos import items as video_items


@pytest.mark.parametrize(
    ("router", "expected_dependencies"),
    [
        (auth.router, (deps.db_deps,)),
        (account.router, (deps.db_deps,)),
        (media_libraries.router, (deps.db_deps,)),
        (downloads.router, (deps.db_deps,)),
        (media_import.router, (deps.db_deps, deps.get_current_user)),
        (status.router, (deps.db_deps, deps.get_current_user)),
        (activity.router, (deps.db_deps, deps.get_current_user)),
        (indexer_settings.router, (deps.db_deps,)),
        (plugins.router, (deps.db_deps, deps.get_current_user)),
        (system_config.router, (deps.db_deps,)),
        (image_search.router, (deps.db_deps, deps.get_current_user)),
        (hot_actress_releases_router, (deps.db_deps, deps.get_current_user)),
        (ranking_sources.router, (deps.db_deps, deps.get_current_user)),
        (tags.router, (deps.db_deps, deps.get_current_user)),
        (movie_subscriptions.router, (deps.db_deps, deps.get_current_user)),
        (video_items.router, (deps.db_deps, deps.get_current_user)),
        (video_collections.router, (deps.db_deps, deps.get_current_user)),
    ],
)
def test_routers_use_expected_router_dependencies(router, expected_dependencies):
    dependency_targets = {
        dependency.dependency for dependency in router.dependencies
    }

    assert set(expected_dependencies) <= dependency_targets


def test_create_app_does_not_register_retired_resource_task_routes():
    paths = {getattr(route, "path", None) for route in create_app().routes}

    assert "/system/resource-task-states/definitions" not in paths
    assert "/system/resource-task-states" not in paths
    assert "/system/resource-task-actions" not in paths


def test_create_app_registers_movie_subscription_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    # 顶层资源而非 /movies 子路径：避免和 /movies/{movie_number} 抢匹配。
    assert "/movie-subscriptions" in paths
    assert "/movie-subscriptions/status-counts" in paths
    assert "/movie-subscriptions/search-resets" in paths
    # 批量取消订阅刻意不在本域：复用已有的 /movies/unsubscriptions 与 DELETE /media/{id}。
    assert "/movie-subscriptions/removals" not in paths


def test_create_app_registers_videos_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/videos" in paths
    assert "/videos/{video_id}" in paths
    assert "/video-collections" in paths
    assert "/video-collections/{collection_id}" in paths
    assert "/video-collections/{collection_id}/items" in paths
    assert "/video-collections/{collection_id}/items/{item_id}" in paths
    assert "/video-collections/{collection_id}/items/reorder" in paths
    assert "/video-imports" not in paths


def test_create_app_registers_only_unified_media_import_route():
    route_methods = {
        (getattr(route, "path", None), method)
        for route in create_app().routes
        for method in getattr(route, "methods", set())
    }

    assert ("/imports", "POST") in route_methods
    assert ("/subtitle-imports", "POST") not in route_methods
    assert not any((path or "").startswith("/imports/") for path, _ in route_methods)


def test_openapi_uses_oauth2_password_flow_for_authorize_button():
    app = create_app()
    schema = app.openapi()

    security_schemes = schema["components"]["securitySchemes"]
    oauth_scheme = security_schemes["OAuth2PasswordBearer"]

    assert oauth_scheme["type"] == "oauth2"
    assert oauth_scheme["flows"]["password"]["tokenUrl"] == "/auth/docs-token"


def test_create_app_registers_file_images_route_without_security_dependencies():
    app = create_app()
    image_route = next(
        route for route in app.routes if getattr(route, "path", None) == "/files/images/{file_path:path}"
    )

    assert image_route.dependant.dependencies == []


def test_create_app_registers_image_search_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/image-search/sessions" in paths
    assert "/image-search/sessions/{session_id}/results" in paths
    assert "/image-search/plot-sessions" in paths
    assert "/image-search/plot-sessions/{session_id}/results" in paths
    assert "/daily-recommendations" in paths
    assert "/moment-recommendations" in paths
    assert "/hot-reviews" not in paths
    assert "/hot-actress-releases" in paths
    assert "/ranking-sources" in paths
    assert "/ranking-sources/{source_key}/boards" in paths
    assert "/ranking-sources/{source_key}/boards/{board_key}/items" in paths
    assert "/tags" in paths
    assert "/movies/{movie_number}/subtitles" in paths
    assert "/movies/{movie_number}/collection-status" in paths
    assert "/movies/{movie_number}/metadata-refresh" in paths
    # 翻译链路已删除；互动同步由常规定时任务负责。
    assert "/movies/{movie_number}/desc-translation" not in paths
    assert "/movies/{movie_number}/interaction-sync" not in paths
    assert "/movies/{movie_number}/heat-recompute" in paths
    assert "/movies/blacklist" in paths
    assert "/movies/series/{series_id}/javdb/import/stream" in paths
    # 翻译链路已整体下线：设置探测端点一并移除。
    assert "/movie-desc-translation-settings/test" not in paths
    assert "/system/activity/bootstrap" in paths
    assert "/system/notifications" in paths
    assert "/system/task-runs" in paths
    assert "/system/task-runs/active" in paths
    assert "/system/events/stream" not in paths
    assert "/status/metadata-providers/{provider}/test" in paths
    assert "/import-sources/browse" in paths
    assert "/imports" in paths
    assert not any(path.startswith("/import-jobs") for path in paths if path)


def test_create_app_registers_media_clip_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/media/{media_id}/clips" in paths
    assert "/media-clips" in paths
    assert "/media-clips/{clip_id}" in paths
    assert "/media-clips/{clip_id}/thumbnails" in paths
    assert "/media-clips/{clip_id}/stream" in paths
    assert "/clip-collections" in paths
    assert "/clip-collections/{collection_id}" in paths
    assert "/clip-collections/{collection_id}/clips" in paths
    assert "/clip-collections/{collection_id}/clips/{clip_id}" in paths


def test_create_app_registers_provider_media_play_gateway_and_removes_legacy_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/media/{media_id}/play/{resource_path:path}" in paths
    assert "/media/merged-play/{resource_path:path}" in paths
    assert "/media/play-url" not in paths
    assert "/media/{media_id}/stream" not in paths
    assert "/media/merged-stream" not in paths
    assert "/media/{media_id}/stream.m3u8" not in paths
    assert "/media-libraries/providers" in paths


def test_create_app_registers_playlist_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/playlists" in paths
    assert "/playlists/{playlist_id}" in paths
    assert "/playlists/{playlist_id}/movies" in paths
    assert "/playlists/{playlist_id}/resolutions" in paths


def test_create_app_does_not_register_removed_media_hls_streams_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/media/{media_id}/hls-streams" not in paths
    assert "/media/{media_id}/hls-streams/{bandwidth}.m3u8" not in paths


async def test_api_error_handler_preserves_response_headers():
    response = await api_error_handler(
        None,
        ApiError(
            503,
            "upstream_unavailable",
            "上游服务暂时不可用",
            response_headers={"Retry-After": "300"},
        ),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "300"


def test_create_app_does_not_register_removed_api_endpoints():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    removed_routes = {
        ("/actors/search/local", "GET"),
        ("/image-search/sessions/{session_id}", "GET"),
        ("/system/notifications/unread-count", "GET"),
        ("/actors/{actor_id}/movies", "GET"),
        ("/system/notifications/{notification_id}/archive", "PATCH"),
        ("/system/resource-task-states/{task_key}/{resource_id}/reset", "POST"),
        # 缩略图不再保存可重置的资源状态。
        ("/system/resource-task-states/media_thumbnail_generation/reset", "POST"),
        ("/download-clients/{client_id}/sync", "POST"),
        ("/download-tasks/{task_id}/import", "POST"),
    }

    assert route_methods.isdisjoint(removed_routes)


def test_create_app_registers_download_task_center_routes():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/download-clients/test", "POST") in route_methods
    assert ("/download-tasks", "GET") in route_methods
    assert ("/download-tasks/stream", "GET") not in route_methods
    assert ("/download-tasks/{task_id}", "DELETE") in route_methods


def test_create_app_runs_runtime_startup_jobs(monkeypatch):
    events = []
    monkeypatch.setattr("src.api.app.ensure_database_ready", lambda: events.append("db.ready"))
    app = create_app()

    with TestClient(app):
        pass

    assert events == ["db.ready"]


def test_create_app_sets_peewee_logger_level_from_settings(monkeypatch):
    peewee_logger = logging.getLogger("peewee")
    original_level = peewee_logger.level
    monkeypatch.setattr("src.api.app.settings.logging.level", "WARNING")

    try:
        create_app()

        assert peewee_logger.level == logging.WARNING
    finally:
        peewee_logger.setLevel(original_level)


def test_db_deps_initializes_database_when_proxy_is_not_ready(monkeypatch):
    fake_database = Mock()
    ensure_database_ready = Mock(return_value=fake_database)
    monkeypatch.setattr(deps, "ensure_database_ready", ensure_database_ready)

    resolved_database = deps.db_deps()

    assert resolved_database is fake_database
    ensure_database_ready.assert_called_once_with()
