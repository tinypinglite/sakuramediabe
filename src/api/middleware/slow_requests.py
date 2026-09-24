"""开发用慢请求日志中间件；开关与阈值见 ``src.common.perf``。"""

from __future__ import annotations

import time

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.common.perf import current_request_perf, new_request_perf, slow_request_ms


class SlowRequestLoggingMiddleware:
    """纯 ASGI 中间件：为请求建立耗时上下文，超阈值时输出含 SQL 统计的汇总日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_perf = new_request_perf(scope["method"], scope["path"])
        token = current_request_perf.set(request_perf)
        status_code = 500
        started_at = time.perf_counter()

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000.0
            current_request_perf.reset(token)
            threshold_ms = slow_request_ms()
            if threshold_ms > 0 and duration_ms >= threshold_ms:
                logger.warning(
                    "slow request method={} path={} status={} duration_ms={:.1f} "
                    "db_ms={:.1f} db_queries={} request_id={}",
                    request_perf.method,
                    request_perf.path,
                    status_code,
                    duration_ms,
                    request_perf.query_ms,
                    request_perf.query_count,
                    request_perf.request_id,
                )
