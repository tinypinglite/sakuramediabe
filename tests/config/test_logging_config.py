import toml

import src.config.config as config_module
from src.config.config import Logging, Settings


def test_logging_defaults_to_info():
    logging_config = Logging()

    assert logging_config.level == "INFO"


def test_settings_loads_logging_level_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "logging": {
                    "level": "DEBUG",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = Settings()

    assert settings.logging.level == "DEBUG"
