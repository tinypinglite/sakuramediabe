"""开发用慢请求中间件的日志内容与请求上下文回归。"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from src.api.app import create_app
from src.api.middleware.slow_requests import SlowRequestLoggingMiddleware
from src.common import perf
from src.common.perf import current_request_perf


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


def _build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(SlowRequestLoggingMiddleware)

    @app.get("/fast")
    def fast():
        return {"ok": True}

    @app.get("/slow")
    def slow():
        time.sleep(0.02)
        return {"ok": True}

    return TestClient(app)


def test_slow_request_logs_summary(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.REQUEST_MS_ENV_KEY, "5")
    with _build_client() as client:
        response = client.get("/slow")

    assert response.status_code == 200
    slow_logs = [message for message in warning_messages if "slow request" in message]
    assert len(slow_logs) == 1
    assert "method=GET" in slow_logs[0]
    assert "path=/slow" in slow_logs[0]
    assert "status=200" in slow_logs[0]
    assert "db_ms=0.0" in slow_logs[0]
    assert "db_queries=0" in slow_logs[0]
    assert "request_id=" in slow_logs[0]


def test_fast_request_not_logged(monkeypatch, warning_messages):
    monkeypatch.setenv(perf.REQUEST_MS_ENV_KEY, "60000")
    with _build_client() as client:
        response = client.get("/fast")

    assert response.status_code == 200
    assert [message for message in warning_messages if "slow request" in message] == []


def test_request_context_visible_in_threadpool_endpoint(monkeypatch):
    seen: list[perf.RequestPerf | None] = []
    app = FastAPI()
    app.add_middleware(SlowRequestLoggingMiddleware)

    @app.get("/context")
    def context_endpoint():
        seen.append(current_request_perf.get())
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/context")

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0].path == "/context"


def test_create_app_registers_middleware_when_enabled(monkeypatch):
    monkeypatch.setenv(perf.ENABLED_ENV_KEY, "1")
    app = create_app()
    assert SlowRequestLoggingMiddleware in [
        middleware.cls for middleware in app.user_middleware
    ]


def test_create_app_skips_middleware_when_disabled(monkeypatch):
    monkeypatch.delenv(perf.ENABLED_ENV_KEY, raising=False)
    app = create_app()
    assert SlowRequestLoggingMiddleware not in [
        middleware.cls for middleware in app.user_middleware
    ]
