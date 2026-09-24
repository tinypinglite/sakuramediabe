"""开发用慢请求/慢 SQL 日志。

默认关闭，设置 ``SAKURAMEDIA_SLOW_LOG=1`` 启用；阈值可用
``SAKURAMEDIA_SLOW_REQUEST_MS``（默认 500）与 ``SAKURAMEDIA_SLOW_SQL_MS``
（默认 250）覆盖，置 0 可单独关闭对应日志。

关闭时 ``query_hooks`` 保持为空列表，peewee 不做任何计时，普通用户零开销。
"""

from __future__ import annotations

import os
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from loguru import logger
from peewee import Database, QueryEvent

ENABLED_ENV_KEY = "SAKURAMEDIA_SLOW_LOG"
REQUEST_MS_ENV_KEY = "SAKURAMEDIA_SLOW_REQUEST_MS"
SQL_MS_ENV_KEY = "SAKURAMEDIA_SLOW_SQL_MS"

DEFAULT_SLOW_REQUEST_MS = 500
DEFAULT_SLOW_SQL_MS = 250

_SQL_MAX_CHARS = 500


@dataclass
class RequestPerf:
    """当前 HTTP 请求的耗时上下文，供慢 SQL 钩子归因。"""

    request_id: str
    method: str
    path: str
    query_count: int = 0
    query_ms: float = 0.0


current_request_perf: ContextVar[RequestPerf | None] = ContextVar(
    "current_request_perf", default=None
)


def slow_log_enabled() -> bool:
    enabled = os.getenv(ENABLED_ENV_KEY, "").strip().lower()
    return enabled in {"1", "true", "yes", "on"}


def slow_request_ms() -> int:
    return _env_ms(REQUEST_MS_ENV_KEY, DEFAULT_SLOW_REQUEST_MS)


def slow_sql_ms() -> int:
    return _env_ms(SQL_MS_ENV_KEY, DEFAULT_SLOW_SQL_MS)


def install_query_hooks(database: Database) -> None:
    """挂载慢 SQL 钩子；未启用时不挂。"""

    if not slow_log_enabled():
        return
    if record_query not in database.query_hooks:
        database.query_hooks.append(record_query)


def new_request_perf(method: str, path: str) -> RequestPerf:
    return RequestPerf(request_id=uuid.uuid4().hex[:8], method=method, path=path)


def record_query(event: QueryEvent) -> None:
    """peewee query_hooks 回调：累加请求内 SQL 统计，超阈值时记警告。"""

    request_perf = current_request_perf.get()
    if request_perf is not None:
        request_perf.query_count += 1
        request_perf.query_ms += event.duration * 1000.0

    duration_ms = event.duration * 1000.0
    threshold_ms = slow_sql_ms()
    if threshold_ms <= 0 or duration_ms < threshold_ms:
        return
    logger.warning(
        "slow query duration_ms={:.1f} request_id={} path={} sql={}",
        duration_ms,
        request_perf.request_id if request_perf is not None else "-",
        request_perf.path if request_perf is not None else "-",
        _compact_sql(event.sql),
    )


def _env_ms(env_key: str, default: int) -> int:
    raw = (os.getenv(env_key) or "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning("Invalid {}={!r}, falling back to {}ms", env_key, raw, default)
        return default


def _compact_sql(sql: str) -> str:
    compact = " ".join(sql.split())
    if len(compact) > _SQL_MAX_CHARS:
        return compact[:_SQL_MAX_CHARS] + "..."
    return compact
