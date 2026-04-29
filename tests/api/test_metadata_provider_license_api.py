import json
from pathlib import Path

import httpx
import pytest
from sakuramedia_metadata_providers.exceptions import MetadataLicenseError
from sakuramedia_metadata_providers.license.state import LicenseStatus

import src.config.config as config_module
from src.config.config import Metadata, Settings
from src.service.system.metadata_provider_license_service import MetadataProviderLicenseService


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


class FakeLicenseClient:
    instances = []
    next_status = LicenseStatus(
        configured=True,
        active=False,
        instance_id="inst_test",
        error_code="license_required",
        message="License activation is required",
    )
    activate_error = None
    renew_error = None
    status_error = None
    written_state_paths = []

    def __init__(self, *, version: str, state_path: str, proxy: str | None = None):
        self.version = version
        self.state_path = state_path
        self.proxy = proxy
        self.activated_codes = []
        self.renew_count = 0
        self.__class__.instances.append(self)

    def status(self):
        if self.__class__.status_error:
            raise self.__class__.status_error
        return self.__class__.next_status

    def activate(self, activation_code: str):
        self.activated_codes.append(activation_code)
        state_path = Path(self.state_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "instance_id": "inst_test",
                    "lease_token": "fake-lease-token",
                    "renew_after_seconds": 21600,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.__class__.written_state_paths.append(str(state_path))
        if self.__class__.activate_error:
            raise self.__class__.activate_error
        return LicenseStatus(
            configured=True,
            active=True,
            instance_id="inst_test",
            expires_at=1777181126,
            license_valid_until=1780000000,
            renew_after_seconds=21600,
        )

    def renew(self):
        self.renew_count += 1
        if self.__class__.renew_error:
            raise self.__class__.renew_error
        return LicenseStatus(
            configured=True,
            active=True,
            instance_id="inst_test",
            expires_at=1777181126,
            license_valid_until=1780000000,
            renew_after_seconds=21600,
        )


@pytest.fixture()
def fake_license_client(monkeypatch):
    FakeLicenseClient.instances = []
    FakeLicenseClient.next_status = LicenseStatus(
        configured=True,
        active=False,
        instance_id="inst_test",
        error_code="license_required",
        message="License activation is required",
    )
    FakeLicenseClient.activate_error = None
    FakeLicenseClient.renew_error = None
    FakeLicenseClient.status_error = None
    FakeLicenseClient.written_state_paths = []
    monkeypatch.setattr(
        "src.service.system.metadata_provider_license_service.LicenseClient",
        FakeLicenseClient,
    )
    return FakeLicenseClient


def _use_temp_provider_license_state(monkeypatch, tmp_path):
    state_path = tmp_path / "provider-license-state.json"
    monkeypatch.setattr(
        "src.metadata.license_runtime.METADATA_LICENSE_STATE_PATH",
        str(state_path),
    )
    return state_path


@pytest.fixture()
def isolated_metadata_settings(tmp_path, monkeypatch):
    original_runtime_settings = Settings.model_validate(config_module.settings.model_dump())
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        json.dumps(original_runtime_settings.model_dump(mode="json")),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)
    config_module.settings.metadata = Metadata(license_proxy="  http://127.0.0.1:7890  ")
    yield config_path
    config_module.refresh_runtime_settings(original_runtime_settings)


def test_metadata_provider_license_endpoints_require_authentication(client):
    get_response = client.get("/metadata-provider-license/status")
    test_response = client.get("/metadata-provider-license/connectivity-test")
    post_response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": "SMB-SECRET-CODE"},
    )
    renew_response = client.post("/metadata-provider-license/renew")

    assert get_response.status_code == 401
    assert get_response.json()["error"]["code"] == "unauthorized"
    assert test_response.status_code == 401
    assert test_response.json()["error"]["code"] == "unauthorized"
    assert post_response.status_code == 401
    assert post_response.json()["error"]["code"] == "unauthorized"
    assert renew_response.status_code == 401
    assert renew_response.json()["error"]["code"] == "unauthorized"


def test_metadata_provider_license_status_returns_sanitized_status(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    monkeypatch,
):
    monkeypatch.setenv("SAKURAMEDIA_BACKEND_VERSION", "v9.9.9")
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "active": False,
        "instance_id": "inst_test",
        "expires_at": None,
        "license_valid_until": None,
        "renew_after_seconds": None,
        "error_code": "license_required",
        "message": "License activation is required",
    }
    assert fake_license_client.instances[-1].version == "v9.9.9"
    assert fake_license_client.instances[-1].state_path == "/data/config/provider-license-state.json"
    assert fake_license_client.instances[-1].proxy == "http://127.0.0.1:7890"


def test_metadata_provider_license_status_returns_inactive_when_state_unavailable(
    client,
    account_user,
    fake_license_client,
):
    fake_license_client.status_error = RuntimeError("broken state")
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["active"] is False
    assert response.json()["error_code"] == "license_unavailable"


def test_metadata_provider_license_status_returns_license_valid_until(
    client,
    account_user,
    fake_license_client,
):
    fake_license_client.next_status = LicenseStatus(
        configured=True,
        active=True,
        instance_id="inst_test",
        expires_at=1777181126,
        license_valid_until=1780000000,
        renew_after_seconds=21600,
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["license_valid_until"] == 1780000000


def test_metadata_provider_license_activate_uses_code_without_persisting_config(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    monkeypatch,
    tmp_path,
):
    _use_temp_provider_license_state(monkeypatch, tmp_path)
    token = _login(client, username=account_user.username)
    secret_code = "SMB-SUPER-SECRET"

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": secret_code},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["active"] is True
    assert response.json()["license_valid_until"] == 1780000000
    assert fake_license_client.instances[-1].activated_codes == [secret_code]
    assert secret_code not in isolated_metadata_settings.read_text(encoding="utf-8")


def test_metadata_provider_license_activate_replaces_state_after_success(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    monkeypatch,
    tmp_path,
):
    state_path = _use_temp_provider_license_state(monkeypatch, tmp_path)
    state_path.write_text('{"lease_token":"old-token"}', encoding="utf-8")
    token = _login(client, username=account_user.username)
    secret_code = "SMB-SUPER-SECRET"

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": secret_code},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "instance_id": "inst_test",
        "lease_token": "fake-lease-token",
        "renew_after_seconds": 21600,
    }
    assert fake_license_client.instances[-1].state_path != str(state_path)
    assert fake_license_client.written_state_paths == [fake_license_client.instances[-1].state_path]
    assert not list(state_path.parent.glob(f".{state_path.name}.activate-*.tmp"))
    assert secret_code not in isolated_metadata_settings.read_text(encoding="utf-8")


def test_metadata_provider_license_activate_keeps_state_after_failure(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    monkeypatch,
    tmp_path,
):
    state_path = _use_temp_provider_license_state(monkeypatch, tmp_path)
    old_state = b'{"lease_token":"old-token"}'
    state_path.write_bytes(old_state)
    fake_license_client.activate_error = MetadataLicenseError(
        "activation_code_invalid",
        "Activation code is invalid",
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": "SMB-BAD-CODE"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "activation_code_invalid"
    assert state_path.read_bytes() == old_state
    assert fake_license_client.instances[-1].state_path != str(state_path)
    assert fake_license_client.written_state_paths == [fake_license_client.instances[-1].state_path]
    assert not list(state_path.parent.glob(f".{state_path.name}.activate-*.tmp"))
    assert "SMB-BAD-CODE" not in isolated_metadata_settings.read_text(encoding="utf-8")


def test_metadata_provider_license_renew_returns_updated_status(
    client,
    account_user,
    fake_license_client,
):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/renew",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["active"] is True
    assert response.json()["expires_at"] == 1777181126
    assert response.json()["license_valid_until"] == 1780000000
    assert fake_license_client.instances[-1].renew_count == 1


def test_metadata_provider_license_connectivity_test_uses_license_proxy(
    client,
    account_user,
    isolated_metadata_settings,
    monkeypatch,
):
    captured = {}

    class FakeHttpClient:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, url: str):
            captured["url"] = url
            return httpx.Response(204)

    monkeypatch.setattr(
        "src.service.system.metadata_provider_license_service.httpx.Client",
        FakeHttpClient,
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/connectivity-test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["url"] == MetadataProviderLicenseService.LICENSE_CENTER_URL
    assert payload["proxy_enabled"] is True
    assert payload["status_code"] == 204
    assert payload["error"] is None
    assert captured["url"] == MetadataProviderLicenseService.LICENSE_CENTER_URL
    assert captured["kwargs"] == {
        "timeout": 10.0,
        "trust_env": False,
        "proxy": "http://127.0.0.1:7890",
    }


def test_metadata_provider_license_connectivity_test_omits_blank_proxy(
    client,
    account_user,
    isolated_metadata_settings,
    monkeypatch,
):
    captured = {}
    config_module.settings.metadata = Metadata(license_proxy="   ")

    class FakeHttpClient:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, url: str):
            return httpx.Response(200)

    monkeypatch.setattr(
        "src.service.system.metadata_provider_license_service.httpx.Client",
        FakeHttpClient,
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/connectivity-test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["proxy_enabled"] is False
    assert "proxy" not in captured["kwargs"]


def test_metadata_provider_license_connectivity_test_returns_sanitized_failure(
    client,
    account_user,
    isolated_metadata_settings,
    monkeypatch,
):
    secret_proxy = "http://user:secret@127.0.0.1:7890"
    config_module.settings.metadata = Metadata(license_proxy=secret_proxy)

    class FakeHttpClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, url: str):
            raise httpx.TimeoutException(f"timeout via {secret_proxy}")

    monkeypatch.setattr(
        "src.service.system.metadata_provider_license_service.httpx.Client",
        FakeHttpClient,
    )
    token = _login(client, username=account_user.username)

    response = client.get(
        "/metadata-provider-license/connectivity-test",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["proxy_enabled"] is True
    assert payload["status_code"] is None
    assert "TimeoutException" in payload["error"]
    assert "secret" not in payload["error"]
    assert secret_proxy not in payload["error"]


@pytest.mark.parametrize(
    ("license_error", "expected_status"),
    [
        (MetadataLicenseError("activation_code_invalid", "Activation code is invalid", {"activation_code": "SMB-SECRET"}), 403),
        (MetadataLicenseError("activation_conflict", "Activation conflict, please retry"), 409),
        (MetadataLicenseError("too_many_requests", "Too many requests"), 429),
        (MetadataLicenseError("license_server_error", "License server returned invalid JSON"), 502),
        (MetadataLicenseError("unknown_worker_error", "Unknown worker error", {"lease_token": "secret"}), 502),
    ],
)
def test_metadata_provider_license_activate_maps_license_errors_without_sensitive_details(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    license_error,
    expected_status,
    monkeypatch,
    tmp_path,
):
    _use_temp_provider_license_state(monkeypatch, tmp_path)
    secret_code = "SMB-SUPER-SECRET"
    fake_license_client.activate_error = license_error
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": secret_code},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == expected_status
    payload = response.json()
    assert payload["error"]["code"] == license_error.code
    assert payload["error"]["details"] == {"license_error_code": license_error.code}
    assert secret_code not in response.text
    assert "lease_token" not in response.text


def test_metadata_provider_license_activate_maps_http_errors_to_unavailable(
    client,
    account_user,
    fake_license_client,
    isolated_metadata_settings,
    monkeypatch,
    tmp_path,
):
    _use_temp_provider_license_state(monkeypatch, tmp_path)
    secret_code = "SMB-SUPER-SECRET"
    fake_license_client.activate_error = httpx.TimeoutException("timeout")
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": secret_code},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "license_unavailable"
    assert secret_code not in response.text


@pytest.mark.parametrize(
    ("license_error", "expected_status"),
    [
        (MetadataLicenseError("license_revoked", "License is revoked", {"lease_token": "secret"}), 403),
        (MetadataLicenseError("version_blocked", "Version is blocked"), 403),
        (MetadataLicenseError("too_many_requests", "Too many requests"), 429),
        (MetadataLicenseError("unknown_worker_error", "Unknown worker error", {"lease_token": "secret"}), 502),
    ],
)
def test_metadata_provider_license_renew_maps_license_errors_without_sensitive_details(
    client,
    account_user,
    fake_license_client,
    license_error,
    expected_status,
):
    fake_license_client.renew_error = license_error
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/renew",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == expected_status
    payload = response.json()
    assert payload["error"]["code"] == license_error.code
    assert payload["error"]["details"] == {"license_error_code": license_error.code}
    assert "lease_token" not in response.text


def test_metadata_provider_license_renew_maps_http_errors_to_unavailable(
    client,
    account_user,
    fake_license_client,
):
    fake_license_client.renew_error = httpx.TimeoutException("timeout with lease_token=secret")
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/renew",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "license_unavailable"
    assert "lease_token" not in response.text


def test_metadata_provider_license_activate_rejects_blank_code(
    client,
    account_user,
    fake_license_client,
):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/metadata-provider-license/activate",
        json={"activation_code": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert fake_license_client.instances == []
