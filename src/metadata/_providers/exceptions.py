class MetadataProviderError(Exception):
    """metadata provider 基础异常。"""


class MetadataNotFoundError(MetadataProviderError):
    def __init__(self, resource: str, lookup_value: str):
        self.resource = resource
        self.lookup_value = lookup_value
        super().__init__(f"{resource} not found: {lookup_value}")


class MetadataRequestError(MetadataProviderError):
    def __init__(self, method: str, url: str, detail: str):
        self.method = method
        self.url = url
        self.detail = detail
        super().__init__(f"metadata request failed: {method} {url} ({detail})")


class JavdbAuthError(MetadataProviderError):
    """JavDB 账号登录失败：用于区分“登录失败”与普通抓取失败。"""

    def __init__(self, detail: str | None = None):
        self.detail = detail or "javdb account login failed"
        super().__init__(f"javdb auth failed: {self.detail}")


class MetadataProviderUnavailable(MetadataProviderError):
    def __init__(self, provider: str, detail: str | None = None):
        self.provider = provider
        self.detail = detail or "provider unavailable"
        super().__init__(f"metadata provider unavailable: {provider} ({self.detail})")
