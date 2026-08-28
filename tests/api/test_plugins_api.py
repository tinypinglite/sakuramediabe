"""插件管理 API 骨架回归：鉴权、zip 上传安装、启停与移除。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

from src.config.config import settings
from src.plugins import HOST_API_VERSION


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _zip_bytes(
    plugin_id: str = "api_plugin",
    version: str = "1.0.0",
    dependencies: list[str] | None = None,
) -> bytes:
    manifest = json.dumps(
        {
            "plugin_id": plugin_id,
            "display_name": "API 演示",
            "version": version,
            "host_api_version": HOST_API_VERSION,
            **({"dependencies": dependencies} if dependencies is not None else {}),
        },
        ensure_ascii=False,
    )
    register = (
        "from src.plugins import HOST_API_VERSION, PluginRegistration\n"
        "def register(context):\n"
        f"    return PluginRegistration(plugin_id='{plugin_id}', display_name='API 演示', "
        f"version='{version}', host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("__init__.py", register)
    return buffer.getvalue()


def test_plugins_endpoints_require_authentication(client):
    response = client.get("/system/plugins")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_install_plugin_from_zip_and_manage(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "plugins"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    monkeypatch.setattr(
        "src.plugins.manager.update_settings",
        lambda new_settings: setattr(
            settings.plugins, "enabled", list(new_settings.plugins.enabled)
        ),
    )
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}
    archive = _zip_bytes()
    sha256 = hashlib.sha256(archive).hexdigest()

    response = client.post(
        "/system/plugins",
        headers=headers,
        files={"file": ("demo.zip", archive, "application/zip")},
        data={"sha256": sha256, "enable": "false"},
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "plugin_id": "api_plugin",
        "version": "1.0.0",
        "pending_restart": ["api", "aps"],
    }

    response = client.get("/system/plugins", headers=headers)
    assert response.status_code == 200
    plugins = response.json()
    assert [item["plugin_id"] for item in plugins] == ["api_plugin"]
    assert plugins[0]["enabled"] is False

    response = client.get("/system/plugins/api_plugin", headers=headers)
    assert response.status_code == 200
    assert response.json()["data_dir"].endswith("data")

    response = client.patch(
        "/system/plugins/api_plugin",
        params={"enabled": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True

    response = client.delete("/system/plugins/api_plugin", headers=headers)
    assert response.status_code == 200
    assert response.json()["plugin_id"] == "api_plugin"

    response = client.get("/system/plugins/api_plugin", headers=headers)
    assert response.status_code == 404


def test_install_plugin_rejects_invalid_zip(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "src.plugins.manager._plugin_root",
        lambda: tmp_path / "plugins",
    )
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/system/plugins",
        headers=headers,
        files={"file": ("bad.zip", b"not a zip", "application/zip")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plugin_install_failed"


def test_install_plugin_with_dependencies_requires_container_restart(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "plugins"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    monkeypatch.setattr(
        "src.plugins.manager.update_settings",
        lambda new_settings: setattr(
            settings.plugins, "enabled", list(new_settings.plugins.enabled)
        ),
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        "/system/plugins",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": (
                "dependency-plugin.zip",
                _zip_bytes(
                    plugin_id="dependency_plugin",
                    dependencies=["missing-package>=1"],
                ),
                "application/zip",
            )
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["pending_restart"] == ["container"]


def test_plugin_settings_endpoints(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "plugins"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    monkeypatch.setattr(
        "src.plugins.manager.update_settings",
        lambda new_settings: setattr(settings, "plugins", new_settings.plugins),
    )
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/system/plugins",
        headers=headers,
        files={"file": ("demo.zip", _zip_bytes(), "application/zip")},
        data={"enable": "true"},
    )
    assert response.status_code == 201

    response = client.get("/system/plugins/api_plugin/settings", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"settings": {}}

    payload = {"overlap_days": 7, "tags": ["4k", "sub"]}
    response = client.put(
        "/system/plugins/api_plugin/settings",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json() == {
        "settings": payload,
        "pending_restart": ["api", "aps"],
    }

    response = client.get("/system/plugins/api_plugin/settings", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"settings": payload}

    response = client.put(
        "/system/plugins/api_plugin/settings",
        headers=headers,
        json={"secret": None, "tags": ["4k", None]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_plugin_settings"
    response = client.get("/system/plugins/api_plugin/settings", headers=headers)
    assert response.json() == {"settings": payload}

    response = client.get("/system/plugins/missing_plugin/settings", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plugin_not_found"

    response = client.put(
        "/system/plugins/missing_plugin/settings",
        headers=headers,
        json={"x": 1},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plugin_not_found"

    # 清理全局配置，避免影响同进程内的其它测试。
    settings.plugins.enabled = []
    settings.plugins.settings = {}
