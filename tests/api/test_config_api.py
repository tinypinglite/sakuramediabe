"""统一配置 API 的持久化与重启语义回归。"""

from __future__ import annotations

import toml

from src.config.config import (
    Settings,
    ensure_runtime_config,
    settings,
    update_settings,
)
from src.service.system.config_service import ConfigService


def _login(client, username: str, password: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_config_update_rejects_removed_javdb_host(
    client,
    account_user,
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.toml"
    original_config_path = Settings.model_config["toml_file"]
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
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
            json={"metadata": {"javdb_host": "config-restart-only.example"}},
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "unknown_config_field"
        assert not config_path.exists()
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
            {"media": {"allowed_min_video_file_size": 1}}
        )
        ConfigService.update_config(
            {
                "scheduler": {
                    "movie_heat_cron": "0 6 * * *",
                    "worker_default_concurrency": 6,
                }
            }
        )
        persisted = toml.load(config_path)
        assert persisted["media"]["allowed_min_video_file_size"] == 1
        assert persisted["scheduler"]["movie_heat_cron"] == "0 6 * * *"
        assert persisted["scheduler"]["worker_default_concurrency"] == 6

        plugin_update = Settings.model_validate(settings.model_dump())
        plugin_update.plugins.settings = {"demo_plugin": {"enabled": True}}
        update_settings(plugin_update)

        persisted = toml.load(config_path)
        assert persisted["media"]["allowed_min_video_file_size"] == 1
        assert persisted["scheduler"]["movie_heat_cron"] == "0 6 * * *"
        assert persisted["plugins"]["settings"] == {"demo_plugin": {"enabled": True}}

        ConfigService.update_config({"media": {"allowed_min_video_file_size": 2}})
        persisted = toml.load(config_path)
        assert persisted["media"]["allowed_min_video_file_size"] == 2
        assert persisted["plugins"]["settings"] == {"demo_plugin": {"enabled": True}}
    finally:
        settings.plugins = original_plugins
        Settings.model_config["toml_file"] = original_config_path


def test_settings_load_config_written_by_v06_with_obsolete_keys(tmp_path, monkeypatch):
    """v0.6.x 写盘时残留的旧键必须能原样加载，不影响升到 v0.7.0。"""
    config_path = tmp_path / "config.toml"
    original_config_path = Settings.model_config["toml_file"]
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    config_path.write_text(
        toml.dumps(
            {
                "media": {
                    "allowed_min_video_file_size": 1,
                    "inner_sub_tags": ["中字"],
                },
                "media_import": {"browse_roots": ["/mnt"]},
                "metadata": {"javdb_host": "custom.example"},
                "unrelated": {"value": "keep"},
            }
        ),
        encoding="utf-8",
    )

    try:
        loaded = Settings()
        assert loaded.media.allowed_min_video_file_size == 1
    finally:
        Settings.model_config["toml_file"] = original_config_path


def test_ensure_runtime_config_preserves_custom_image_search_endpoint(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    original_config_path = Settings.model_config["toml_file"]
    monkeypatch.setitem(Settings.model_config, "toml_file", config_path)
    monkeypatch.setattr(settings.auth, "secret_key", "test-secret")
    monkeypatch.setattr(settings.auth, "file_signature_secret", "test-file-secret")
    custom_url = "https://embedding.example.test/v1"
    config_path.write_text(
        toml.dumps({"image_search": {"inference_base_url": custom_url}}),
        encoding="utf-8",
    )

    try:
        assert ensure_runtime_config() is False
        assert toml.load(config_path)["image_search"]["inference_base_url"] == custom_url
    finally:
        Settings.model_config["toml_file"] = original_config_path
