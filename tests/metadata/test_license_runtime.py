import os

from src.config.config import settings
from src.metadata.license_runtime import (
    BACKEND_VERSION_DEFAULT,
    METADATA_LICENSE_STATE_PATH,
    resolve_metadata_provider_license_runtime,
)


def test_license_runtime_uses_default_version_and_tmp_state(monkeypatch):
    monkeypatch.delenv("SAKURAMEDIA_BACKEND_VERSION", raising=False)
    monkeypatch.delenv("PROVIDER_LICENSE_CODE", raising=False)
    monkeypatch.delenv("PROVIDER_LICENSE_PROXY", raising=False)
    monkeypatch.delenv("PROVIDER_LICENSE_STATE_PATH", raising=False)
    monkeypatch.setattr(settings.metadata, "license_proxy", None)

    runtime = resolve_metadata_provider_license_runtime()

    assert runtime.version == BACKEND_VERSION_DEFAULT
    assert runtime.state_path == METADATA_LICENSE_STATE_PATH
    assert runtime.license_proxy is None


def test_license_runtime_reads_backend_version_and_configured_license_proxy(monkeypatch):
    monkeypatch.setenv("SAKURAMEDIA_BACKEND_VERSION", " v1.2.3 ")
    monkeypatch.setattr(settings.metadata, "license_proxy", "  http://127.0.0.1:7890  ")

    runtime = resolve_metadata_provider_license_runtime()

    assert runtime.version == "v1.2.3"
    assert runtime.license_proxy == "http://127.0.0.1:7890"


def test_license_runtime_ignores_provider_license_environment_variables(monkeypatch):
    monkeypatch.setenv("PROVIDER_LICENSE_CODE", "SHOULD-NOT-BE-READ")
    monkeypatch.setenv("PROVIDER_LICENSE_PROXY", "http://127.0.0.1:9000")
    monkeypatch.setenv("PROVIDER_LICENSE_STATE_PATH", "/data/config/license.json")
    monkeypatch.setattr(settings.metadata, "license_proxy", None)

    runtime = resolve_metadata_provider_license_runtime()

    assert runtime.state_path == METADATA_LICENSE_STATE_PATH
    assert runtime.license_proxy is None
    assert os.getenv("PROVIDER_LICENSE_CODE") == "SHOULD-NOT-BE-READ"
