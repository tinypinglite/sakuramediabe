from fastapi.testclient import TestClient
from unittest.mock import Mock
import logging

from src.api.routers import deps
from src.api.routers.catalog import tags
from src.api.routers.files import images
from src.api.routers.discovery import hot_reviews
from src.api.routers.discovery import image_search
from src.api.routers.discovery import ranking_sources
from src.api.routers.playback import media_libraries
from src.api.routers.system import account
from src.api.routers.system import activity
from src.api.routers.system import auth
from src.api.routers.system import collection_number_features
from src.api.routers.system import indexer_settings
from src.api.routers.system import movie_desc_translation_settings
from src.api.routers.system import status
from src.api.routers.transfers import downloads
from src.api.app import create_app
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


def test_collection_number_features_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in collection_number_features.router.dependencies
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
    assert "/movie-desc-translation-settings" in paths
    assert "/movie-desc-translation-settings/test" in paths
    assert "/collection-number-features" in paths
    assert "/system/activity/bootstrap" in paths
    assert "/system/notifications" in paths
    assert "/system/task-runs" in paths
    assert "/system/events/stream" in paths
    assert "/status/metadata-providers/{provider}/test" in paths
    assert "/metadata-provider-license/status" in paths
    assert "/metadata-provider-license/connectivity-test" in paths
    assert "/metadata-provider-license/activate" in paths
    assert "/metadata-provider-license/renew" in paths


def test_create_app_does_not_register_removed_api_endpoints():
    app = create_app()
    route_methods = {
        (getattr(route, "path", None), method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    removed_routes = {
        ("/actors/search/local", "GET"),
        ("/media", "GET"),
        ("/image-search/sessions/{session_id}", "GET"),
        ("/system/notifications/unread-count", "GET"),
        ("/system/task-runs/active", "GET"),
        ("/actors/{actor_id}/movies", "GET"),
        ("/system/notifications/{notification_id}/archive", "PATCH"),
        ("/system/resource-task-states/{task_key}/{resource_id}/reset", "POST"),
        ("/download-clients/{client_id}/sync", "POST"),
        ("/download-tasks", "GET"),
        ("/download-tasks/{task_id}/import", "POST"),
        ("/download-tasks", "DELETE"),
    }

    assert route_methods.isdisjoint(removed_routes)


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
