import configparser
from pathlib import Path


def load_supervisord_config() -> configparser.RawConfigParser:
    config = configparser.RawConfigParser()
    config.read(
        Path(__file__).resolve().parents[2] / "supervisord.conf",
        encoding="utf-8",
    )
    return config


def test_api_and_aps_logs_persist_under_data_logs():
    config = load_supervisord_config()

    # stderr 合并进 stdout，只有一套轮转逻辑，因此只校验 stdout_logfile。
    assert config.get("program:api", "stdout_logfile") == "/data/logs/api.log"
    assert config.getboolean("program:api", "redirect_stderr") is True
    assert config.get("program:aps", "stdout_logfile") == "/data/logs/aps.log"
    assert config.getboolean("program:aps", "redirect_stderr") is True
