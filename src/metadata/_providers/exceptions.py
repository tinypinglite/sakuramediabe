class MetadataProviderError(Exception):
    """metadata provider 基础异常。"""


class MetadataNotFoundError(MetadataProviderError):
    def __init__(self, resource: str, lookup_value: str):
        self.resource = resource
        self.lookup_value = lookup_value
        super().__init__(f"{resource} not found: {lookup_value}")


class MetadataRequestError(MetadataProviderError):
    def __init__(
        self,
        method: str,
        url: str,
        detail: str,
        *,
        status_code: int | None = None,
    ):
        self.method = method
        self.url = url
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"metadata request failed: {method} {url} ({detail})")


class MetadataProviderUnavailable(MetadataProviderError):
    def __init__(self, provider: str, detail: str | None = None):
        self.provider = provider
        self.detail = detail or "provider unavailable"
        super().__init__(f"metadata provider unavailable: {provider} ({self.detail})")
