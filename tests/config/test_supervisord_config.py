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

    assert config.get("program:api", "stdout_logfile") == "/data/logs/api.stdout.log"
    assert config.get("program:api", "stderr_logfile") == "/data/logs/api.stderr.log"
    assert config.get("program:aps", "stdout_logfile") == "/data/logs/aps.stdout.log"
    assert config.get("program:aps", "stderr_logfile") == "/data/logs/aps.stderr.log"
