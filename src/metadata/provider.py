"""元数据请求基础能力与闭源 provider 异常导出。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx
from loguru import logger
from sakuramedia_metadata_providers.exceptions import (
    MetadataLicenseError,
    MetadataNotFoundError,
    MetadataProviderError,
    MetadataProviderUnavailable,
    MetadataRequestError,
)

MetadataError = MetadataProviderError


class MetadataRequestClient:
    DEFAULT_TIMEOUT = 10.0
    DEFAULT_MAX_RETRIES = 3
    RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        proxy: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        client_kwargs: Dict[str, Any] = {"trust_env": False}
        if proxy:
            client_kwargs["proxy"] = proxy
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(timeout=timeout, **client_kwargs)
        logger.info(
            "MetadataRequestClient initialized timeout={} max_retries={} proxy_enabled={}",
            timeout,
            max_retries,
            bool(proxy),
        )

    def request_json(
        self,
        method: str,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return self._request(method, url, data=data, params=params, headers=headers).json()

    def request_text(
        self,
        method: str,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._request(method, url, data=data, params=params, headers=headers).text

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        request_headers = self.build_request_headers()
        if headers:
            request_headers.update(headers)
        last_exception: Exception | None = None
        for attempt in range(self.max_retries + 1):
            start_at = time.time()
            logger.debug(
                "Metadata request start method={} url={} attempt={}/{}",
                method.upper(),
                url,
                attempt + 1,
                self.max_retries + 1,
            )
            try:
                response = self.client.request(
                    method,
                    url,
                    headers=request_headers,
                    data=data,
                    params=params,
                )
                response.raise_for_status()
                elapsed_ms = int((time.time() - start_at) * 1000)
                logger.info(
                    "Metadata request success method={} url={} status={} elapsed_ms={}",
                    method.upper(),
                    url,
                    response.status_code,
                    elapsed_ms,
                )
                return response
            except httpx.HTTPStatusError as exc:
                last_exception = exc
                elapsed_ms = int((time.time() - start_at) * 1000)
                logger.warning(
                    "Metadata request http error method={} url={} status={} elapsed_ms={} detail={}",
                    method.upper(),
                    url,
                    exc.response.status_code,
                    elapsed_ms,
                    exc,
                )
                if exc.response.status_code not in self.RETRYABLE_STATUS_CODES:
                    raise MetadataRequestError(method, url, str(exc)) from exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exception = exc
                elapsed_ms = int((time.time() - start_at) * 1000)
                logger.warning(
                    "Metadata request transient error method={} url={} elapsed_ms={} detail={}",
                    method.upper(),
                    url,
                    elapsed_ms,
                    exc,
                )
            if attempt >= self.max_retries:
                break
            time.sleep(min(0.5 * (attempt + 1), 2.0))

        logger.error(
            "Metadata request failed after retries method={} url={} detail={}",
            method.upper(),
            url,
            last_exception,
        )
        raise MetadataRequestError(method, url, str(last_exception)) from last_exception

    def build_request_headers(self) -> Dict[str, str]:
        return {}


__all__ = [
    "MetadataError",
    "MetadataLicenseError",
    "MetadataNotFoundError",
    "MetadataProviderError",
    "MetadataProviderUnavailable",
    "MetadataRequestClient",
    "MetadataRequestError",
]
