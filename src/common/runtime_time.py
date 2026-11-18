from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_RUNTIME_TIMEZONE = "Asia/Shanghai"


def _build_fallback_timezone() -> ZoneInfo:
    return ZoneInfo(DEFAULT_RUNTIME_TIMEZONE)


def _timezone_from_env() -> tzinfo | None:
    raw_timezone = (os.getenv("TZ") or "").strip()
    if not raw_timezone:
        return None
    try:
        return ZoneInfo(raw_timezone)
    except ZoneInfoNotFoundError:
        return None


def _timezone_from_system() -> tzinfo | None:
    try:
        # 系统本地时区会自动吸收容器或宿主机的时区配置。
        return datetime.now().astimezone().tzinfo
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_runtime_timezone() -> tzinfo:
    runtime_timezone = _timezone_from_env()
    if runtime_timezone is not None:
        return runtime_timezone

    runtime_timezone = _timezone_from_system()
    if runtime_timezone is not None:
        return runtime_timezone

    return _build_fallback_timezone()


def clear_runtime_timezone_cache() -> None:
    cache_clear = getattr(get_runtime_timezone, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def get_runtime_timezone_name() -> str:
    runtime_timezone = get_runtime_timezone()
    for attr_name in ("key", "zone"):
        timezone_name = getattr(runtime_timezone, attr_name, None)
        if isinstance(timezone_name, str) and timezone_name.strip():
            return timezone_name

    try:
        timezone_name = runtime_timezone.tzname(datetime.now(timezone.utc))
    except Exception:
        timezone_name = None
    return timezone_name or DEFAULT_RUNTIME_TIMEZONE


def to_db_utc_naive(value: datetime, *, assume_tz: tzinfo = timezone.utc) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=assume_tz).astimezone(timezone.utc).replace(tzinfo=None)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_now_for_db() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def runtime_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(get_runtime_timezone())


def to_runtime_local_naive(value: datetime) -> datetime:
    aware_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware_value.astimezone(get_runtime_timezone()).replace(tzinfo=None)


def serialize_runtime_local(value: datetime) -> str:
    return to_runtime_local_naive(value).isoformat(timespec="seconds")


def parse_external_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_db_utc_naive(value)

    normalized = value.strip()
    if not normalized:
        return None
    # 兼容常见的 Z 结尾时间戳，统一收口到数据库使用的 naive UTC。
    normalized = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return to_db_utc_naive(parsed, assume_tz=timezone.utc)
    return to_db_utc_naive(parsed)


def serialize_runtime_local_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_runtime_local(value)
    return value
