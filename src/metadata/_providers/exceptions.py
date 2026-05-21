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


class MetadataProviderUnavailable(MetadataProviderError):
    def __init__(self, provider: str, detail: str | None = None):
        self.provider = provider
        self.detail = detail or "provider unavailable"
        super().__init__(f"metadata provider unavailable: {provider} ({self.detail})")


class MissavThumbnailError(MetadataProviderError):
    pass


class MissavThumbnailNotFoundError(MissavThumbnailError):
    def __init__(self, movie_number: str, detail: str | None = None):
        self.movie_number = movie_number
        self.detail = detail or "thumbnail config not found"
        super().__init__(f"missav thumbnail not found: {movie_number} ({self.detail})")


class MissavThumbnailRequestError(MissavThumbnailError):
    def __init__(self, url: str, detail: str):
        self.url = url
        self.detail = detail
        super().__init__(f"missav thumbnail request failed: {url} ({detail})")


class MissavRankingError(MetadataProviderError):
    pass


class MissavRankingRequestError(MissavRankingError):
    def __init__(self, url: str, detail: str):
        self.url = url
        self.detail = detail
        super().__init__(f"missav ranking request failed: {url} ({detail})")
