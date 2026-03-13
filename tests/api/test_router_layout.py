from fastapi.testclient import TestClient
from unittest.mock import Mock
import logging

from src.api.routers import deps
from src.api.routers.files import images
from src.api.routers.discovery import image_search
from src.api.routers.playback import media_libraries
from src.api.routers.system import account
from src.api.routers.system import auth
from src.api.routers.system import indexer_settings
from src.api.routers.system import status
from src.api.routers.transfers import downloads
from src.api.app import create_app


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


def test_indexer_settings_router_uses_db_deps_as_router_level_dependency():
    assert hasattr(deps, "db_deps")
    assert any(
        isinstance(dependency.dependency, type(deps.db_deps))
        or dependency.dependency is deps.db_deps
        for dependency in indexer_settings.router.dependencies
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
    app = create_app(run_initdb_on_startup=False)
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/image-search/sessions" in paths
    assert "/image-search/sessions/{session_id}" in paths
    assert "/image-search/sessions/{session_id}/results" in paths


def test_create_app_runs_initdb_on_startup(monkeypatch):
    startup_initdb = Mock()
    monkeypatch.setattr("src.api.app.initdb", startup_initdb)

    app = create_app()

    with TestClient(app):
        pass

    startup_initdb.assert_called_once_with()


def test_create_app_can_skip_initdb_on_startup(monkeypatch):
    startup_initdb = Mock()
    monkeypatch.setattr("src.api.app.initdb", startup_initdb)

    app = create_app(run_initdb_on_startup=False)

    with TestClient(app):
        pass

    startup_initdb.assert_not_called()


def test_create_app_sets_peewee_logger_level_from_settings(monkeypatch):
    peewee_logger = logging.getLogger("peewee")
    original_level = peewee_logger.level
    monkeypatch.setattr("src.api.app.settings.logging.level", "WARNING")

    try:
        create_app(run_initdb_on_startup=False)

        assert peewee_logger.level == logging.WARNING
    finally:
        peewee_logger.setLevel(original_level)


def test_db_deps_initializes_database_when_proxy_is_not_ready(monkeypatch):
    fake_database = Mock()
    fake_database.is_closed.return_value = False

    init_database = Mock(return_value=fake_database)

    def raise_uninitialized():
        raise RuntimeError("Database has not been initialized")

    monkeypatch.setattr(deps, "get_database", raise_uninitialized)
    monkeypatch.setattr(deps, "init_database", init_database)

    resolved_database = deps.db_deps()

    assert resolved_database is fake_database
    init_database.assert_called_once()
