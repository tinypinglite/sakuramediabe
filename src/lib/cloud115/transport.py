from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115OfflineQuotaExceededError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    Cloud115RiskControlError,
)
from src.lib.cloud115.session import Cloud115Session

_AUTH_ERRNOS = frozenset({50003, 50004, 99, 911, 20130827, 99999, 990009, 990017})
_NOT_FOUND_ERRNOS = frozenset({20121, 20125, 990002, 4100003, 4100008})
_REQUEST_ERRNOS = frozenset({990005})
_MEMBERSHIP_REQUIRED_ERRNOS = frozenset({406})
_OFFLINE_QUOTA_EXCEEDED_ERRNOS = frozenset({10004, 10008})


def _safe_endpoint(url: str) -> str:
    """日志只保留主机和路径，避免查询参数中的敏感信息落盘。"""
    parsed = urlsplit(url)
    return f"{parsed.netloc}{parsed.path or '/'}"


def _safe_action(
    params: dict[str, Any] | None,
    data: dict[str, Any] | bytes | None,
) -> str:
    """仅记录 115 的固定动作名，不记录完整请求参数。"""
    for values in (params, data if isinstance(data, dict) else None):
        if values and values.get("ac"):
            return str(values["ac"])[:64]
    return "-"


def _safe_detail(value: Any) -> str:
    return " ".join(str(value).split())[:200] or "-"


class Cloud115Transport:
    """统一 HTTP 生命周期、cookies 合并、限速、重试与异常映射。"""

    _MAX_RETRIES = 2
    _RETRY_BACKOFF_STEP = 0.5

    def __init__(
        self,
        session: Cloud115Session,
        *,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        min_request_interval: float = 1.0,
    ) -> None:
        self.session = session
        self._min_request_interval = max(0.0, min_request_interval)
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def _base_headers(self) -> dict[str, str]:
        return {
            "Cookie": self.session.snapshot_cookies(),
            "User-Agent": self.session.user_agent,
        }

    @property
    def _cookies_lock(self):
        return self.session.lock

    def _merge_set_cookies(self, response: httpx.Response) -> None:
        self.session.merge_set_cookies(response)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request_raw(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = self._base_headers()
        if headers:
            request_headers.update(headers)
        await self._acquire_request_slot()
        response = await self._client.request(
            method, url, params=params, headers=request_headers
        )
        async with self.session.lock:
            self.session.merge_set_cookies(response)
        return response

    async def _acquire_request_slot(self) -> None:
        """全局请求限速闸门：保证相邻请求（含重试）间隔 >= _min_request_interval。

        在锁内"预定"下一个发起时刻后立即释放锁再 sleep，避免持锁睡眠阻塞并发协程；
        因此即使多个协程并发调用，也能得到严格匀速的发起节奏。
        """
        if self._min_request_interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            start_at = max(now, self._next_request_at)
            self._next_request_at = start_at + self._min_request_interval
            delay = start_at - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool | None = None,
    ) -> httpx.Response:
        """带退避重试的 HTTP 请求，返回 2xx httpx.Response。上层再自行 .json() 或 .text。

        - 429：直接抛 Cloud115RateLimitedError（不重试，避免账号信号升级）
        - 5xx / TimeoutException / NetworkError：仅幂等请求最多 2 次退避重试
        - 4xx（401/403）：401/403 → Cloud115AuthError；其它 → Cloud115RequestError

        每次成功响应到达后会 merge Set-Cookie 到内部 cookies dict（保活 acw_tc 等）。
        401/403 抛异常前也会 merge（可能带着新 acw_tc / logout 信号）。
        """
        last_error: Exception | None = None
        should_retry = retryable if retryable is not None else method.upper() in {"GET", "HEAD", "OPTIONS"}
        max_retries = self._MAX_RETRIES if should_retry else 0
        endpoint = _safe_endpoint(url)
        action = _safe_action(params, data)
        for attempt in range(max_retries + 1):
            # 每次重试前重新拼 headers（因为上一次响应可能 merge 了新的 acw_tc）
            request_headers = self._base_headers()
            if headers:
                request_headers.update(headers)
            try:
                request_kwargs: dict[str, Any] = {
                    "params": params,
                    "headers": request_headers,
                }
                if isinstance(data, bytes):
                    request_kwargs["content"] = data
                else:
                    request_kwargs["data"] = data
                # 限速闸门：紧贴真正的网络发起，重试也各自受限（避免退避后瞬时补偿式突发）
                await self._acquire_request_slot()
                response = await self._client.request(method, url, **request_kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                logger.warning(
                    "cloud115 request transient error method={} endpoint={} action={} "
                    "attempt={}/{} detail={}",
                    method, endpoint, action, attempt + 1, max_retries + 1,
                    _safe_detail(exc),
                )
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._RETRY_BACKOFF_STEP * (attempt + 1))
                continue

            # 无论后续是否抛异常，Set-Cookie 都可以 merge（服务端可能同时塞新 acw_tc + 拒绝请求）
            async with self._cookies_lock:
                self._merge_set_cookies(response)

            status = response.status_code
            if not (200 <= status < 300):
                logger.warning(
                    "cloud115 http failure method={} endpoint={} action={} status={} "
                    "attempt={}/{} detail={}",
                    method, endpoint, action, status, attempt + 1, max_retries + 1,
                    _safe_detail(response.reason_phrase),
                )
            if status == 429:
                # 限流：立刻抛，携带 Retry-After（不做重试，避免账号触发更严格的风控）
                retry_after = response.headers.get("Retry-After")
                retry_after_int = int(retry_after) if retry_after and retry_after.isdigit() else None
                raise Cloud115RateLimitedError(
                    f"429 rate limited on {method} {url}",
                    retry_after_seconds=retry_after_int,
                )
            if status in (401, 403):
                raise Cloud115AuthError(
                    f"http {status} on {method} {url}", endpoint=url
                )
            if status == 405:
                # 裸 HTTP 405 来自 webapi 前置的阿里云 WAF（不是 115 应用层 state=false+errno）：
                # 账号/cookie 已被风控冻结。不重试、抛专用异常，让上层立即熔断停批。
                raise Cloud115RiskControlError(
                    f"http 405 (risk control / WAF) on {method} {url}",
                    method=method,
                    url=url,
                )
            if 500 <= status < 600:
                # 5xx：退避重试
                last_error = Cloud115RequestError(
                    f"http {status} on {method} {url}",
                    method=method,
                    url=url,
                    detail=response.text[:200],
                )
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._RETRY_BACKOFF_STEP * (attempt + 1))
                continue
            if not (200 <= status < 300):
                raise Cloud115RequestError(
                    f"http {status} on {method} {url}",
                    method=method,
                    url=url,
                    detail=response.text[:200],
                )
            return response

        # 重试耗尽
        detail = str(last_error) if last_error else "unknown"
        raise Cloud115RequestError(
            f"request failed after {max_retries + 1} attempts: {method} {url} ({detail})",
            method=method,
            url=url,
            detail=detail,
        ) from last_error

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        """_request 的 JSON 便捷封装。2xx 但非 JSON 抛 Cloud115RequestError。"""
        response = await self._request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            retryable=retryable,
        )
        try:
            payload = response.json()
        except Exception as exc:
            logger.warning(
                "cloud115 invalid json method={} endpoint={} action={} status={} detail={}",
                method, _safe_endpoint(url), _safe_action(params, data),
                response.status_code, _safe_detail(exc),
            )
            raise Cloud115RequestError(
                f"non-json body on {method} {url}",
                method=method,
                url=url,
                detail=str(exc),
            ) from exc
        if payload.get("state") is False:
            message = (
                payload.get("error")
                or payload.get("error_msg")
                or payload.get("message")
                or payload.get("msg")
                or "unknown"
            )
            logger.warning(
                "cloud115 api rejected method={} endpoint={} action={} errno={} "
                "errcode={} detail={}",
                method, _safe_endpoint(url), _safe_action(params, data),
                payload.get("errno") or payload.get("errNo") or payload.get("code"),
                payload.get("errcode"), _safe_detail(message),
            )
        return payload

    async def _get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET 文本资源；用于读取 HLS playlist。"""
        response = await self._request("GET", url, headers=headers)
        return response.text

    @staticmethod
    def _map_errno(payload: dict[str, Any], *, endpoint: str) -> Cloud115Error:
        """把 state=False 的响应按 errno 映射到具体异常子类。"""
        errno = payload.get("errno") or payload.get("errNo") or payload.get("code")
        message = (
            payload.get("error")
            or payload.get("error_msg")
            or payload.get("message")
            or payload.get("msg")
            or "unknown"
        )
        try:
            errno_int = int(errno) if errno is not None else None
        except (TypeError, ValueError):
            errno_int = None

        if errno_int in _AUTH_ERRNOS:
            return Cloud115AuthError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _MEMBERSHIP_REQUIRED_ERRNOS:
            return Cloud115MembershipRequiredError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _OFFLINE_QUOTA_EXCEEDED_ERRNOS:
            return Cloud115OfflineQuotaExceededError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _NOT_FOUND_ERRNOS:
            return Cloud115NotFoundError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _REQUEST_ERRNOS:
            return Cloud115RequestError(
                f"{message} (errno={errno_int})",
                method=None,
                url=endpoint,
                detail=message,
                errno=errno_int,
            )
        # 未识别的 errno：不静默吞，回到基类让上层看到
        return Cloud115Error(
            f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
        )
