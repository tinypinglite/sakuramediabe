"""元数据请求基础异常与 HTTP 客户端代理。"""

from __future__ import annotations

from src.metadata._providers.exceptions import (
    MetadataNotFoundError,
    MetadataProviderError,
    MetadataProviderUnavailable,
    MetadataRequestError,
)
from src.metadata._providers.http_client import MetadataRequestClient

MetadataError = MetadataProviderError

__all__ = [
    "MetadataError",
    "MetadataNotFoundError",
    "MetadataProviderError",
    "MetadataProviderUnavailable",
    "MetadataRequestClient",
    "MetadataRequestError",
]
