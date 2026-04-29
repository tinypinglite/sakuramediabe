import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.common import runtime_time


def test_get_runtime_timezone_prefers_tz_env(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    if hasattr(time, "tzset"):
        time.tzset()
    runtime_time.clear_runtime_timezone_cache()

    assert runtime_time.get_runtime_timezone_name() == "Asia/Tokyo"


def test_get_runtime_timezone_falls_back_to_system(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    runtime_time.clear_runtime_timezone_cache()
    monkeypatch.setattr(runtime_time, "_timezone_from_env", lambda: None)
    monkeypatch.setattr(runtime_time, "_timezone_from_system", lambda: ZoneInfo("Europe/Berlin"))

    assert runtime_time.get_runtime_timezone_name() == "Europe/Berlin"


def test_get_runtime_timezone_falls_back_to_shanghai(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    runtime_time.clear_runtime_timezone_cache()
    monkeypatch.setattr(runtime_time, "_timezone_from_env", lambda: None)
    monkeypatch.setattr(runtime_time, "_timezone_from_system", lambda: None)

    assert runtime_time.get_runtime_timezone_name() == "Asia/Shanghai"


def test_serialize_runtime_local_converts_naive_utc(monkeypatch):
    monkeypatch.setattr(runtime_time, "get_runtime_timezone", lambda: ZoneInfo("Asia/Shanghai"))

    assert runtime_time.serialize_runtime_local(datetime(2026, 3, 28, 0, 0, 0)) == "2026-03-28T08:00:00"


def test_serialize_runtime_local_converts_aware_utc(monkeypatch):
    monkeypatch.setattr(runtime_time, "get_runtime_timezone", lambda: ZoneInfo("Asia/Shanghai"))

    assert (
        runtime_time.serialize_runtime_local(datetime(2026, 3, 28, 0, 0, 0, tzinfo=timezone.utc))
        == "2026-03-28T08:00:00"
    )


def test_parse_external_datetime_normalizes_z_suffix():
    assert runtime_time.parse_external_datetime("2026-03-28T00:00:00Z") == datetime(2026, 3, 28, 0, 0, 0)
