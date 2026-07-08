import json

import pytest
import toml

import src.config.config as config_module
from src.config.config import Settings


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    original_runtime_settings = Settings.model_validate(config_module.settings.model_dump())
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(json.loads(original_runtime_settings.model_dump_json())),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def _persisted(config_path) -> dict:
    return toml.load(config_path)


def test_config_endpoints_require_authentication(client):
    # 用未鉴权可通过前置校验的常规请求体，避免同时触发 401 与 readonly_config_key 的语义模糊。
    assert client.get("/config").status_code == 401
    assert client.patch("/config", json={"metadata": {"proxy": ""}}).status_code == 401


def test_get_config_returns_values_and_effects(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.get("/config", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert "database" in body["values"]
    # 只读键（auth 节 / enable_docs 顶层字段）整体不经此接口暴露。
    assert "auth" not in body["values"]
    assert "auth" not in body["effects"]
    assert "enable_docs" not in body["values"]
    assert "enable_docs" not in body["effects"]
    assert body["effects"]["scheduler"] == "restart_scheduler"
    assert body["effects"]["metadata"] == "hot"


def test_patch_readonly_auth_returns_422(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"auth": {"username": "foo"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "readonly_config_key"


def test_patch_readonly_enable_docs_returns_422(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"enable_docs": True},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "readonly_config_key"


def test_patch_hot_field_applies_and_persists(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"metadata": {"proxy": "socks5://127.0.0.1:1080"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == ["metadata.proxy"]
    assert body["pending_restart"] == []
    assert config_module.settings.metadata.proxy == "socks5://127.0.0.1:1080"
    assert _persisted(isolated_config)["metadata"]["proxy"] == "socks5://127.0.0.1:1080"


def test_patch_cron_field_reports_pending_restart(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"scheduler": {"movie_heat_cron": "0 6 * * *"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == []
    assert body["pending_restart"] == [
        {"field": "scheduler.movie_heat_cron", "restart": "scheduler"}
    ]


def test_patch_unknown_field_returns_422(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"metadata": {"typo_field": "x"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_config_field"


def test_patch_invalid_cron_returns_422(client, account_user, isolated_config):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/config",
        headers={"Authorization": f"Bearer {token}"},
        json={"scheduler": {"movie_heat_cron": "not a cron"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_config_value"
