import json

import pytest

from src.config.config import Settings
from src.model import Media, MediaLibrary, Movie
from src.service.system.telemetry_service import TelemetryService


def test_instance_id_is_created_once_next_to_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, "toml_file", tmp_path / "config.toml")

    first_instance_id = TelemetryService._load_or_create_instance_id()
    second_instance_id = TelemetryService._load_or_create_instance_id()

    assert first_instance_id == second_instance_id
    assert json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8")) == {
        "instance_id": first_instance_id
    }


def test_report_posts_heartbeat(monkeypatch):
    monkeypatch.delenv(TelemetryService.ENABLED_ENV_KEY, raising=False)
    payload = {
        "schema_version": 2,
        "instance_id": "550e8400-e29b-41d4-a716-446655440000",
        "backend_version": "v0.5.3",
        "plugins": [],
        "platform": "linux",
        "cpu_architecture": "amd64",
        "managed_media_file_count": 0,
        "managed_media_total_bytes": 0,
    }
    sent: dict[str, object] = {}

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

    def post(url: str, *, json: dict[str, object], timeout: float) -> Response:
        sent.update(url=url, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(TelemetryService, "_build_payload", lambda: payload)
    monkeypatch.setattr("src.service.system.telemetry_service.httpx.post", post)

    TelemetryService.report()

    assert sent == {
        "url": TelemetryService.ENDPOINT,
        "json": payload,
        "timeout": 10.0,
    }


def test_build_payload_reports_only_valid_managed_media(test_db, monkeypatch):
    library = MediaLibrary.create(name="telemetry-library", provider_key="test", provider_config={})
    valid_movie = Movie.create(
        movie_number="TELEMETRY-001",
        javdb_id="telemetry-1",
        title="valid media",
    )
    invalid_movie = Movie.create(
        movie_number="TELEMETRY-002",
        javdb_id="telemetry-2",
        title="invalid media",
    )
    Media.create(
        movie=valid_movie,
        library=library,
        file_name="valid.mp4",
        file_size_bytes=100,
        valid=True,
    )
    Media.create(
        movie=invalid_movie,
        library=library,
        file_name="invalid.mp4",
        file_size_bytes=200,
        valid=False,
    )
    monkeypatch.setattr(
        TelemetryService,
        "_load_or_create_instance_id",
        lambda: "550e8400-e29b-41d4-a716-446655440000",
    )
    monkeypatch.setattr(
        "src.service.system.telemetry_service.PluginManager",
        lambda: type("PluginManager", (), {"list_plugins": lambda self: []})(),
    )
    monkeypatch.setattr("src.service.system.telemetry_service.platform.system", lambda: "Linux")
    monkeypatch.setattr("src.service.system.telemetry_service.platform.machine", lambda: "aarch64")

    assert TelemetryService._build_payload() == {
        "schema_version": 2,
        "instance_id": "550e8400-e29b-41d4-a716-446655440000",
        "backend_version": "dev-local",
        "plugins": [],
        "platform": "linux",
        "cpu_architecture": "aarch64",
        "managed_media_file_count": 1,
        "managed_media_total_bytes": 100,
    }


def test_build_payload_reports_zero_metrics_for_empty_library(test_db):
    assert TelemetryService._managed_media_metrics() == (0, 0)


@pytest.mark.parametrize(
    ("system", "machine", "expected_platform", "expected_architecture"),
    [
        ("Linux", "x86_64", "linux", "x86_64"),
        ("Darwin", "aarch64", "darwin", "aarch64"),
        ("Windows", "armv7l", "windows", "armv7l"),
    ],
)
def test_runtime_platform_and_cpu_architecture_are_reported_without_normalization(
    monkeypatch,
    system,
    machine,
    expected_platform,
    expected_architecture,
):
    monkeypatch.setattr("src.service.system.telemetry_service.platform.system", lambda: system)
    monkeypatch.setattr("src.service.system.telemetry_service.platform.machine", lambda: machine)

    assert TelemetryService._runtime_platform() == expected_platform
    assert TelemetryService._cpu_architecture() == expected_architecture


def test_disabled_report_does_not_send_or_create_instance_id(tmp_path, monkeypatch):
    monkeypatch.setenv(TelemetryService.ENABLED_ENV_KEY, " false ")
    monkeypatch.setitem(Settings.model_config, "toml_file", tmp_path / "config.toml")
    monkeypatch.setattr(
        "src.service.system.telemetry_service.httpx.post",
        lambda *args, **kwargs: pytest.fail("disabled telemetry must not post"),
    )

    TelemetryService.report()

    assert not (tmp_path / "telemetry.json").exists()
