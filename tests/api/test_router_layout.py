import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from starlette.requests import Request

from src.api.app import create_app
from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers import deps
from src.api.routers.catalog import subscriptions as movie_subscriptions
from src.api.routers.catalog import subtitle_imports, tags
from src.api.routers.discovery import hot_reviews, image_search, ranking_sources
from src.api.routers.files import images
from src.api.routers.playback import media as media_router
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
from src.api.routers.transfers import downloads, media_import, rapid_uploads
from src.api.routers.videos import collections as video_collections
from src.api.routers.videos import items as video_items
from src.service.playback.cloud115_hls_service import Cloud115HlsService
from src.service.playback.media_service import MediaService


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


def test_subtitle_imports_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency for dependency in subtitle_imports.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_rapid_uploads_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in rapid_uploads.router.dependencies
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


def test_create_app_does_not_register_retired_resource_task_routes():
    paths = {getattr(route, "path", None) for route in create_app().routes}

    assert "/system/resource-task-states/definitions" not in paths
    assert "/system/resource-task-states" not in paths
    assert "/system/resource-task-actions" not in paths


def test_indexer_settings_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in indexer_settings.router.dependencies
    )


def test_plugins_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency for dependency in plugins.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


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


def test_movie_subscriptions_router_uses_auth_and_db_dependencies():
    dependency_targets = {
        dependency.dependency
        for dependency in movie_subscriptions.router.dependencies
    }

    assert deps.db_deps in dependency_targets
    assert deps.get_current_user in dependency_targets


def test_create_app_registers_movie_subscription_routes():
    app = create_app()
    paths = {getattr(route, "path", None) for route in app.routes}

    # 顶层资源而非 /movies 子路径：避免和 /movies/{movie_number} 抢匹配。
    assert "/movie-subscriptions" in paths
    assert "/movie-subscriptions/status-counts" in paths
    assert "/movie-subscriptions/search-resets" in paths
    # 批量取消订阅刻意不在本域：复用已有的 /movies/unsubscriptions 与 DELETE /media/{id}。
    assert "/movie-subscriptions/removals" not in paths


def test_videos_routers_use_auth_and_db_dependencies():
    for module in (video_items, video_collections):
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
    assert "/video-imports" not in paths


def test_create_app_registers_only_unified_media_import_route():
    route_methods = {
        (getattr(route, "path", None), method)
        for route in create_app().routes
        for method in getattr(route, "methods", set())
    }

    assert ("/imports", "POST") in route_methods
    assert not any((path or "").startswith("/imports/") for path, _ in route_methods)


def test_create_app_registers_subtitle_import_routes():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/subtitle-imports", "POST") in route_methods
    assert ("/subtitle-imports", "GET") not in route_methods
    assert not any(
        (path or "").startswith("/subtitle-imports/{subtitle_import_job_id}")
        for path, _method in route_methods
    )


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
    assert "/movies/{movie_number}/subtitles" in paths
    assert "/movies/{movie_number}/collection-status" in paths
    assert "/movies/{movie_number}/metadata-refresh" in paths
    # 翻译链路已删除；互动同步由常规定时任务负责。
    assert "/movies/{movie_number}/desc-translation" not in paths
    assert "/movies/{movie_number}/interaction-sync" not in paths
    assert "/movies/{movie_number}/heat-recompute" in paths
    assert "/movies/series/{series_id}/javdb/import/stream" in paths
    # 翻译链路已整体下线：设置探测端点一并移除。
    assert "/movie-desc-translation-settings/test" not in paths
    assert "/system/activity/bootstrap" in paths
    assert "/system/notifications" in paths
    assert "/system/task-runs" in paths
    assert "/system/events/stream" not in paths
    assert "/status/metadata-providers/{provider}/test" in paths
    assert "/filesystem/entries" in paths
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


async def test_cloud115_stream_uses_smart_playback_dispatch(monkeypatch):
    media = SimpleNamespace(id=5048)
    monkeypatch.setattr(media_router, "verify_media_signature", Mock())
    monkeypatch.setattr(media_router.Media, "get_or_none", Mock(return_value=media))
    monkeypatch.setattr(
        media_router.MediaService,
        "is_cloud115_media",
        Mock(return_value=True),
    )
    captured = {}

    async def resolve_playback(_media, user_agent, signature):
        captured.update(
            media=_media,
            user_agent=user_agent,
            signature=signature,
        )
        return "https://cpats01.115.com/highest.m3u8"

    monkeypatch.setattr(
        media_router.MediaService,
        "resolve_cloud115_playback_url",
        resolve_playback,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/media/5048/stream",
            "headers": [(b"user-agent", b"Frontend-Player/2.0")],
            "client": ("127.0.0.1", 1234),
        }
    )

    response = await media_router.stream_media_file(
        request=request,
        media_id=5048,
        expires=1700000900,
        signature="signed",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://cpats01.115.com/highest.m3u8"
    assert response.headers["cache-control"] == "no-store"
    assert captured == {
        "media": media,
        "user_agent": "Frontend-Player/2.0",
        "signature": "signed",
    }


async def test_cloud115_playback_prefers_highest_hls(monkeypatch):
    media = SimpleNamespace(id=5048)
    hls_resolver = AsyncMock(return_value="https://cdn/highest.m3u8")
    direct_resolver = AsyncMock(return_value="https://cdn/original.mp4")
    monkeypatch.setattr(
        Cloud115HlsService,
        "resolve_highest_variant_url",
        hls_resolver,
    )
    monkeypatch.setattr(MediaService, "resolve_cloud115_stream_url", direct_resolver)

    result = await MediaService.resolve_cloud115_playback_url(
        media,
        "Frontend-Player/2.0",
        "signed",
    )

    assert result == "https://cdn/highest.m3u8"
    hls_resolver.assert_awaited_once_with(media, user_agent="Frontend-Player/2.0")
    direct_resolver.assert_not_awaited()


async def test_cloud115_hls_dispatch_selects_highest_bandwidth(monkeypatch):
    media = SimpleNamespace(id=5049)
    video_info = SimpleNamespace(
        definitions=[
            SimpleNamespace(bandwidth=900000, m3u8_url="https://cdn/720p.m3u8"),
            SimpleNamespace(bandwidth=3000000, m3u8_url="https://cdn/1080p.m3u8"),
            SimpleNamespace(bandwidth=600000, m3u8_url="https://cdn/540p.m3u8"),
        ]
    )
    monkeypatch.setattr(Cloud115HlsService, "_highest_variant_cache", {})
    monkeypatch.setattr(
        Cloud115HlsService,
        "_require_pickcode",
        Mock(return_value="pickcode"),
    )
    video_info_resolver = AsyncMock(return_value=video_info)
    monkeypatch.setattr(
        Cloud115HlsService,
        "_get_video_info",
        video_info_resolver,
    )

    result = await Cloud115HlsService.resolve_highest_variant_url(
        media,
        user_agent="Frontend-Player/2.0",
    )

    assert result == "https://cdn/1080p.m3u8"
    video_info_resolver.assert_awaited_once_with(
        media,
        "pickcode",
        user_agent="Frontend-Player/2.0",
    )


async def test_cloud115_playback_falls_back_for_recoverable_hls_error(monkeypatch):
    media = SimpleNamespace(id=5048)
    hls_resolver = AsyncMock(
        side_effect=ApiError(
            503,
            "cloud115_video_transcoding",
            "115 视频正在转码，请稍后再试",
        )
    )
    direct_resolver = AsyncMock(return_value="https://cdn/original.mp4")
    monkeypatch.setattr(
        Cloud115HlsService,
        "resolve_highest_variant_url",
        hls_resolver,
    )
    monkeypatch.setattr(MediaService, "resolve_cloud115_stream_url", direct_resolver)

    result = await MediaService.resolve_cloud115_playback_url(
        media,
        "Frontend-Player/2.0",
        "signed",
    )

    assert result == "https://cdn/original.mp4"
    direct_resolver.assert_awaited_once_with(
        media,
        "Frontend-Player/2.0",
        "signed",
    )


async def test_cloud115_playback_preserves_cookie_error(monkeypatch):
    media = SimpleNamespace(id=5048)
    cookie_error = ApiError(
        422,
        "cloud115_cookies_invalid",
        "115 cookies 已失效，请重新扫码登录",
    )
    hls_resolver = AsyncMock(side_effect=cookie_error)
    direct_resolver = AsyncMock(return_value="https://cdn/original.mp4")
    monkeypatch.setattr(
        Cloud115HlsService,
        "resolve_highest_variant_url",
        hls_resolver,
    )
    monkeypatch.setattr(MediaService, "resolve_cloud115_stream_url", direct_resolver)

    try:
        await MediaService.resolve_cloud115_playback_url(
            media,
            "Frontend-Player/2.0",
            "signed",
        )
    except ApiError as exc:
        assert exc is cookie_error
    else:
        raise AssertionError("cloud115 cookie error must not fall back to direct stream")
    direct_resolver.assert_not_awaited()


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
        # 缩略图不再保存可重置的资源状态。
        ("/system/resource-task-states/media_thumbnail_generation/reset", "POST"),
        ("/download-clients/{client_id}/sync", "POST"),
        ("/download-tasks/{task_id}/import", "POST"),
    }

    assert route_methods.isdisjoint(removed_routes)


def test_create_app_registers_rapid_uploads_routes():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/media/rapid-uploads", "POST") in route_methods
    assert ("/media/rapid-uploads", "GET") in route_methods
    assert ("/media/rapid-uploads/{batch_id}", "GET") in route_methods
    assert ("/media/rapid-uploads/{batch_id}/retry", "POST") in route_methods


def test_create_app_registers_download_task_center_routes():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("/download-tasks", "GET") in route_methods
    assert ("/download-tasks/stream", "GET") not in route_methods
    assert ("/download-tasks/{task_id}/files", "GET") in route_methods
    assert ("/download-tasks/{task_id}/pause", "POST") in route_methods
    assert ("/download-tasks/{task_id}/resume", "POST") in route_methods
    assert ("/download-tasks/{task_id}", "DELETE") in route_methods


def test_create_app_runs_runtime_startup_jobs(monkeypatch):
    events = []
    monkeypatch.setattr("src.api.app.ensure_database_ready", lambda: events.append("db.ready"))
    startup_recover = Mock(side_effect=lambda **kwargs: events.append(("recover", kwargs)) or [])
    monkeypatch.setattr("src.api.app.recover_interrupted_tasks", startup_recover)
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
