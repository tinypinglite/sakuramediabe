"""开发用慢日志（src.common.perf）的开关、阈值与日志内容回归。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger as loguru_logger
from peewee import PostgresqlDatabase, QueryEvent

from src.common import perf
from src.config.config import Database
from src.model.base import create_database


@pytest.fixture()
def warning_messages() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = loguru_logger.add(
        lambda message: messages.append(str(message)),
        level="WARNING",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(sink_id)


def _event(duration_s: float, sql: str = "SELECT 1") -> QueryEvent:
    return QueryEvent(sql=sql, params=(), duration=duration_s, exception=None)


def test_slow_log_disabled_by_default(monkeypatch):
    monkeypatch.delenv(perf.ENABLED_ENV_KEY, raising=False)
    database = PostgresqlDatabase("unused")
    perf.install_query_hooks(database)
    assert perf.slow_log_enabled() is False
    assert database.query_hooks == []


def test_slow_log_enabled_installs_hook(monkeypatch):
    monkeypatch.setenv(perf.ENABLED_ENV_KEY, "On")
    database = PostgresqlDatabase("unused")
    perf.install_query_hooks(database)
    assert perf.slow_log_enabled() is True
    assert perf.record_query in database.query_hooks


def test_create_database_installs_hook_when_enabled(monkeypatch):
    monkeypatch.setenv(perf.ENABLED_ENV_KEY, "1")
    database = create_database(Database())
    assert perf.record_query in database.query_hooks


def test_record_query_logs_over_threshold_and_accumulates(
    monkeypatch,
    warning_messages,
):
    monkeypatch.setenv(perf.SQL_MS_ENV_KEY, "250")
    request_perf = perf.new_request_perf("GET", "/movies")
    token = perf.current_request_perf.set(request_perf)
    try:
        perf.record_query(_event(0.4, sql="SELECT * FROM movie\nWHERE id = %s"))
        perf.record_query(_event(0.1))
    finally:
        perf.current_request_perf.reset(token)

    assert request_perf.query_count == 2
    assert request_perf.query_ms == pytest.approx(500.0)
    slow_logs = [message for message in warning_messages if "slow query" in message]
    assert len(slow_logs) == 1
    assert "duration_ms=400.0" in slow_logs[0]
    assert f"request_id={request_perf.request_id}" in slow_logs[0]
    assert "path=/movies" in slow_logs[0]
    assert "sql=SELECT * FROM movie WHERE id = %s" in slow_logs[0]


def test_record_query_without_request_context(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.SQL_MS_ENV_KEY, "100")
    perf.record_query(_event(0.2))
    slow_logs = [message for message in warning_messages if "slow query" in message]
    assert len(slow_logs) == 1
    assert "request_id=-" in slow_logs[0]
    assert "path=-" in slow_logs[0]


def test_record_query_does_not_log_params(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.SQL_MS_ENV_KEY, "100")
    event = QueryEvent(
        sql="SELECT * FROM user WHERE token = %s",
        params=("secret-token",),
        duration=0.2,
        exception=None,
    )
    perf.record_query(event)
    assert "slow query" in warning_messages[-1]
    assert "secret-token" not in "".join(warning_messages)


def test_sql_threshold_zero_disables_log(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.SQL_MS_ENV_KEY, "0")
    perf.record_query(_event(5.0))
    assert [message for message in warning_messages if "slow query" in message] == []


def test_invalid_threshold_falls_back_to_default(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.SQL_MS_ENV_KEY, "abc")
    assert perf.slow_sql_ms() == perf.DEFAULT_SLOW_SQL_MS
    assert any("Invalid" in message for message in warning_messages)
