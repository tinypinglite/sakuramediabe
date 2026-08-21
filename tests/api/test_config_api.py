"""统一配置 API 的持久化与重启语义回归。"""

from __future__ import annotations

import toml

from src.config.config import Settings, settings, update_settings
from src.service.system.config_service import ConfigService


def _login(client, username: str, password: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_config_update_persists_without_refreshing_runtime_settings(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.toml"
    original_config_path = Settings.model_config["toml_file"]
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    original_host = settings.metadata.javdb_host
    updated_host = "config-restart-only.example"
    headers = {
        "Authorization": f"Bearer {_login(client, account_user.username, 'password123')}"
    }

    try:
        current = client.get("/config", headers=headers)
        assert current.status_code == 200
        assert "effects" not in current.json()

        response = client.patch(
            "/config",
            headers=headers,
            json={"metadata": {"javdb_host": updated_host}},
        )

        assert response.status_code == 200, response.text
        result = response.json()
        assert result["values"]["metadata"]["javdb_host"] == updated_host
        assert result["restart_required"] == ["api", "aps"]
        # 当前进程继续使用启动快照，避免 API 与 APS 的配置观察不一致。
        assert settings.metadata.javdb_host == original_host
        assert toml.load(config_path)["metadata"]["javdb_host"] == updated_host
        assert not list(tmp_path.glob(".config.toml.*.tmp"))
    finally:
        Settings.model_config["toml_file"] = original_config_path


def test_config_updates_merge_from_disk_and_survive_plugin_updates(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    original_config_path = Settings.model_config["toml_file"]
    original_plugins = settings.plugins.model_copy(deep=True)
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)

    try:
        ConfigService.update_config(
            {"metadata": {"javdb_host": "first-update.example"}}
        )
        ConfigService.update_config(
            {"scheduler": {"movie_heat_cron": "0 6 * * *"}}
        )
        persisted = toml.load(config_path)
        assert persisted["metadata"]["javdb_host"] == "first-update.example"
        assert persisted["scheduler"]["movie_heat_cron"] == "0 6 * * *"

        plugin_update = Settings.model_validate(settings.model_dump())
        plugin_update.plugins.settings = {"demo_plugin": {"enabled": True}}
        update_settings(plugin_update)

        persisted = toml.load(config_path)
        assert persisted["metadata"]["javdb_host"] == "first-update.example"
        assert persisted["scheduler"]["movie_heat_cron"] == "0 6 * * *"
        assert persisted["plugins"]["settings"] == {"demo_plugin": {"enabled": True}}

        ConfigService.update_config({"metadata": {"javdb_host": "second-update.example"}})
        persisted = toml.load(config_path)
        assert persisted["metadata"]["javdb_host"] == "second-update.example"
        assert persisted["plugins"]["settings"] == {"demo_plugin": {"enabled": True}}
    finally:
        settings.plugins = original_plugins
        Settings.model_config["toml_file"] = original_config_path


def test_legacy_download_progress_poll_settings_are_ignored_on_startup():
    loaded = Settings.model_validate(
        {
            "downloads": {
                "progress_stream_poll_interval_seconds": 0.5,
                "cloud115_progress_poll_interval_seconds": 8.0,
            }
        }
    )

    assert "progress_stream_poll_interval_seconds" not in loaded.downloads.model_dump()
    assert "cloud115_progress_poll_interval_seconds" not in loaded.downloads.model_dump()
    assert loaded.scheduler.download_progress_snapshot_interval_seconds == 5.0
