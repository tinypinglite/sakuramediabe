from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.model import Media


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


class ThumbnailBackendUnavailable(RuntimeError):
    """后端或账号级故障；停止当前泳道，不能把系统故障计到每条媒体头上。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "thumbnail_backend_unavailable",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class PreparedThumbnailSource:
    source_label: str
    expected_count: int
    payload: Any


@dataclass(frozen=True)
class ThumbnailGenerationResult:
    first_error: Exception | None = None


class ThumbnailBackend(Protocol):
    key: str

    def prepare(self, media: Media) -> PreparedThumbnailSource: ...

    def generate(
        self,
        prepared: PreparedThumbnailSource,
        output_dir: Path,
    ) -> ThumbnailGenerationResult: ...
