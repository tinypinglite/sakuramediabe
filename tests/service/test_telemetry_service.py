import json

from src.config.config import Settings
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
    payload = {
        "schema_version": 1,
        "instance_id": "550e8400-e29b-41d4-a716-446655440000",
        "backend_version": "v0.5.3",
        "plugins": [],
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
