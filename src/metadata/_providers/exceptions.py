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


class JavdbAuthError(MetadataProviderError):
    """JavDB 账号登录失败：用于区分“登录失败”与普通抓取失败。"""

    def __init__(self, detail: str | None = None):
        self.detail = detail or "javdb account login failed"
        super().__init__(f"javdb auth failed: {self.detail}")


class JavdbAuthRequiredError(MetadataProviderError):
    """JavDB 受保护接口要求登录，或当前登录态已失效。"""

    def __init__(self, detail: str | None = None, *, account_configured: bool = False):
        self.detail = detail or "javdb auth required"
        self.account_configured = account_configured
        super().__init__(f"javdb auth required: {self.detail}")


class MetadataProviderUnavailable(MetadataProviderError):
    def __init__(self, provider: str, detail: str | None = None):
        self.provider = provider
        self.detail = detail or "provider unavailable"
        super().__init__(f"metadata provider unavailable: {provider} ({self.detail})")


def metadata_error_code(exc: BaseException) -> str:
    """把元数据异常映射为跨 API/SSE/前端共用的稳定错误码。"""

    if isinstance(exc, JavdbAuthRequiredError):
        return "metadata_auth_required"
    if isinstance(exc, JavdbAuthError):
        return "metadata_auth_failed"
    if isinstance(exc, MetadataNotFoundError):
        return "metadata_not_found"
    if isinstance(exc, MetadataRequestError):
        return "metadata_request_error"
    return "unexpected_error"
