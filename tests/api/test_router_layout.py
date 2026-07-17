from fastapi.testclient import TestClient
from starlette.requests import Request
from unittest.mock import Mock
import logging
from types import SimpleNamespace

from src.api.routers import deps
from src.api.routers.catalog import tags
from src.api.routers.files import images
from src.api.routers.discovery import hot_reviews
from src.api.routers.discovery import image_search
from src.api.routers.discovery import ranking_sources
from src.api.routers.playback import media as media_router
from src.api.routers.playback import media_libraries
from src.api.routers.system import account
from src.api.routers.system import activity
from src.api.routers.system import auth
from src.api.routers.system import config as system_config
from src.api.routers.system import indexer_settings
from src.api.routers.system import movie_desc_translation_settings
from src.api.routers.system import status
from src.api.routers.transfers import downloads
from src.api.routers.transfers import media_import
from src.api.routers.videos import collections as video_collections
from src.api.routers.videos import imports as video_imports
from src.api.routers.videos import items as video_items
from src.api.app import create_app
from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.config.config import settings


def test_auth_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in auth.router.dependencies
    )


def test_account_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in account.router.dependencies
    )


def test_media_libraries_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in media_libraries.router.dependencies
    )


def test_downloads_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in downloads.router.dependencies
    )


def test_media_import_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in media_import.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_status_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in status.router.dependencies
    )


def test_activity_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in activity.router.dependencies
    )


def test_indexer_settings_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in indexer_settings.router.dependencies
    )


def test_movie_desc_translation_settings_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in movie_desc_translation_settings.router.dependencies
    )


def test_config_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in system_config.router.dependencies
    )


def test_file_images_router_has_no_router_level_dependencies():
    assert images.router.dependencies == []


def test_image_search_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in image_search.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_hot_reviews_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in hot_reviews.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_ranking_sources_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in ranking_sources.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_tags_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in tags.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_videos_routers_use_auth_and_db_dependencies():
    for module in (video_items, video_collections, video_imports):
        dependency_targets = {
            dependency.dependency for dependency in module.router.dependencies
        }
        assert deps.db_deps in dependency_targets
        assert deps.get_current_user in dependency_targets


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
    assert "/video-imports" in paths


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
    assert "/daily-recommendations" in paths
    assert "/moment-recommendations" in paths
    assert "/hot-reviews" in paths
    assert "/ranking-sources" in paths
    assert "/ranking-sources/{source_key}/boards" in paths
    assert "/ranking-sources/{source_key}/boards/{board_key}/items" in paths
    assert "/tags" in paths
    assert "/tags/{tag_id}" in paths
    assert "/tags/{tag_id}/movies" in paths
    assert "/movies/{movie_number}/subtitles" in paths
    assert "/movies/{movie_number}/collection-status" in paths
    assert "/movies/{movie_number}/metadata-refresh" in paths
    assert "/movies/{movie_number}/desc-translation" in paths
    assert "/movies/{movie_number}/interaction-sync" in paths
    assert "/movies/{movie_number}/heat-recompute" in paths
    assert "/movies/series/{series_id}/javdb/import/stream" in paths
    # GET/PATCH 已剔除；此路由现在只暴露连通性探测端点。
    assert "/movie-desc-translation-settings/test" in paths
    assert "/system/activity/bootstrap" in paths
    assert "/system/notifications" in paths
    assert "/system/task-runs" in paths
    assert "/system/events/stream" in paths
    assert "/status/metadata-providers/{provider}/test" in paths
    assert "/filesystem/entries" in paths
    assert "/import-jobs" in paths
    assert "/import-jobs/{import_job_id}" in paths
    assert "/import-jobs/{import_job_id}/retry" in paths
    assert "/import-jobs/{import_job_id}/failed-files" in paths
    assert "/import-jobs/{import_job_id}/failed-files/rename" in paths


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


def test_create_app_registers_media_hls_streams_route():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/media/{media_id}/hls-streams" in paths
    assert "/media/{media_id}/hls-streams/{bandwidth}.m3u8" in paths


async def test_hls_stream_list_returns_backend_redirect_urls(monkeypatch):
    media = SimpleNamespace(id=5048)
    monkeypatch.setattr(media_router, "verify_media_signature", Mock())
    monkeypatch.setattr(media_router.Media, "get_or_none", Mock(return_value=media))
    monkeypatch.setattr(
        media_router.MediaService,
        "is_cloud115_media",
        Mock(return_value=True),
    )

    async def list_streams(_media):
        assert _media is media
        return [SimpleNamespace(quality="UD", resolution="1920x1080", bandwidth=3000000)]

    monkeypatch.setattr(
        media_router.Cloud115HlsService,
        "list_hls_streams",
        list_streams,
    )

    response = await media_router.list_media_hls_streams(
        media_id=5048,
        expires=1700000900,
        signature="signed",
    )

    assert response.streams[0].url == (
        "/media/5048/hls-streams/3000000.m3u8"
        "?expires=1700000900&signature=signed"
    )


async def test_hls_redirect_resolves_variant_with_player_user_agent(monkeypatch):
    media = SimpleNamespace(id=5048)
    monkeypatch.setattr(media_router, "verify_media_signature", Mock())
    monkeypatch.setattr(media_router.Media, "get_or_none", Mock(return_value=media))
    captured = {}

    async def resolve_variant(_media, *, bandwidth, user_agent):
        captured.update(
            media=_media,
            bandwidth=bandwidth,
            user_agent=user_agent,
        )
        return "https://cpats01.115.com/video.m3u8"

    monkeypatch.setattr(
        media_router.Cloud115HlsService,
        "resolve_hls_variant_url",
        resolve_variant,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/media/5048/hls-streams/3000000.m3u8",
            "headers": [(b"user-agent", b"Frontend-Player/2.0")],
        }
    )

    response = await media_router.redirect_media_hls_stream(
        request=request,
        media_id=5048,
        bandwidth=3000000,
        expires=1700000900,
        signature="signed",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://cpats01.115.com/video.m3u8"
    assert response.headers["cache-control"] == "no-store"
    assert captured == {
        "media": media,
        "bandwidth": 3000000,
        "user_agent": "Frontend-Player/2.0",
    }


async def test_api_error_handler_preserves_response_headers():
    response = await api_error_handler(
        None,
        ApiError(
            503,
            "cloud115_video_transcoding",
            "115 视频正在转码，请稍后再试",
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
        ("/system/task-runs/active", "GET"),
        ("/actors/{actor_id}/movies", "GET"),
        ("/system/notifications/{notification_id}/archive", "PATCH"),
        ("/system/resource-task-states/{task_key}/{resource_id}/reset", "POST"),
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

    assert ("/download-tasks", "GET") in route_methods
    assert ("/download-tasks/stream", "GET") in route_methods
    assert ("/download-tasks/{task_id}/pause", "POST") in route_methods
    assert ("/download-tasks/{task_id}/resume", "POST") in route_methods
    assert ("/download-tasks/{task_id}", "DELETE") in route_methods


def test_create_app_runs_runtime_startup_jobs(monkeypatch):
    events = []
    monkeypatch.setattr("src.api.app.ensure_database_ready", lambda: events.append("db.ready"))
    startup_recover = Mock(side_effect=lambda **kwargs: events.append(("recover", kwargs)) or [])
    monkeypatch.setattr("src.api.app.recover_interrupted_tasks", startup_recover)
    monkeypatch.setattr(
        "src.start.recovery.DownloadSyncService.recover_orphaned_imports_only",
        lambda self: (_ for _ in ()).throw(AssertionError("should not recover import state")),
    )

    app = create_app()

    with TestClient(app):
        pass

    startup_recover.assert_called_once_with(
        trigger_types=("startup", "manual", "internal"),
        error_message="API进程重启，任务已中断",
    )
    assert events == [
        "db.ready",
        (
            "recover",
            {
                "trigger_types": ("startup", "manual", "internal"),
                "error_message": "API进程重启，任务已中断",
            },
        ),
    ]


def test_create_app_initializes_database_proxy_before_runtime_startup_jobs(monkeypatch):
    events = []
    monkeypatch.setattr("src.api.app.ensure_database_ready", lambda: events.append("db.ready"))
    monkeypatch.setattr(
        "src.api.app.recover_interrupted_tasks",
        Mock(side_effect=lambda **kwargs: events.append("recover") or []),
    )

    app = create_app()

    with TestClient(app):
        pass

    assert events == [
        "db.ready",
        "recover",
    ]


def test_create_app_recovers_task_related_business_running_states_on_startup(monkeypatch):
    events = []

    def fake_recover_interrupted_task_runs(**kwargs):
        events.append(("recover", kwargs["trigger_type"]))
        if kwargs["trigger_type"] == "startup":
            return [type("TaskRun", (), {"task_key": "movie_desc_sync"})()]
        if kwargs["trigger_type"] == "manual":
            return [type("TaskRun", (), {"task_key": "movie_desc_translation"})()]
        if kwargs["trigger_type"] == "internal":
            return [type("TaskRun", (), {"task_key": "download_task_import"})()]
        return []

    monkeypatch.setattr("src.start.recovery.ActivityService.recover_interrupted_task_runs", fake_recover_interrupted_task_runs)
    monkeypatch.setattr(
        "src.start.recovery.MovieDescSyncService.recover_interrupted_running_movies",
        lambda **kwargs: events.append(("recover_desc", kwargs["error_message"])) or 1,
    )
    monkeypatch.setattr(
        "src.start.recovery.MovieDescTranslationService.recover_interrupted_running_movies",
        lambda **kwargs: events.append(("recover_translation", kwargs["error_message"])) or 1,
    )
    monkeypatch.setattr(
        "src.start.recovery.DownloadSyncService.recover_orphaned_imports_only",
        lambda self: events.append(("recover_import", True)) or {"recovered_count": 1},
    )

    app = create_app()

    with TestClient(app):
        pass

    assert events == [
        ("recover", "startup"),
        ("recover", "manual"),
        ("recover", "internal"),
        ("recover_desc", "影片描述抓取任务中断，等待重试"),
        ("recover_translation", "影片简介翻译任务中断，等待重试"),
        ("recover_import", True),
    ]


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
