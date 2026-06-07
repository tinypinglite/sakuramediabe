import toml

import src.config.config as config_module
from src.config.config import MediaImport, Settings


def test_media_import_browse_roots_defaults():
    media_import = MediaImport()

    assert media_import.browse_roots == ["/mnt"]


def test_settings_use_default_media_import_when_config_missing(tmp_path, monkeypatch):
    missing_config_path = tmp_path / "missing-config.toml"
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", missing_config_path)

    settings = Settings()

    assert settings.media_import == MediaImport()


def test_settings_loads_media_import_browse_roots_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps({"media_import": {"browse_roots": ["/mnt/media", "/data/incoming"]}}),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = Settings()

    assert settings.media_import.browse_roots == ["/mnt/media", "/data/incoming"]
