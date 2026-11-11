"""元数据异常重导出与 MetadataRequestClient 代理。"""

from __future__ import annotations

from src.metadata._providers.exceptions import (
    JavdbAuthError,
    JavdbAuthRequiredError,
    MetadataNotFoundError,
    MetadataProviderError,
    MetadataProviderUnavailable,
    MetadataRequestError,
    metadata_error_code,
)
from src.metadata._providers.http_client import MetadataRequestClient

MetadataError = MetadataProviderError

__all__ = [
    "JavdbAuthError",
    "JavdbAuthRequiredError",
    "MetadataError",
    "MetadataNotFoundError",
    "MetadataProviderError",
    "MetadataProviderUnavailable",
    "MetadataRequestClient",
    "MetadataRequestError",
    "metadata_error_code",
]
