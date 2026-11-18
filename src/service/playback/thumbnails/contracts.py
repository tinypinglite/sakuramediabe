class ThumbnailDeferred(RuntimeError):
    """媒体源暂未就绪；必须带有限次退避策略，不能无限 pending。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "thumbnail_source_deferred",
        max_deferred_attempts: int,
        deferred_backoff_base_seconds: int,
    ) -> None:
        if max_deferred_attempts <= 0:
            raise ValueError("max_deferred_attempts_must_be_positive")
        if deferred_backoff_base_seconds <= 0:
            raise ValueError("deferred_backoff_base_seconds_must_be_positive")
        super().__init__(message)
        self.error_code = error_code
        self.max_deferred_attempts = max_deferred_attempts
        self.deferred_backoff_base_seconds = deferred_backoff_base_seconds


__all__ = ["ThumbnailDeferred"]
