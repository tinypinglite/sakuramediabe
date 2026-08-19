from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.model import Media


class ThumbnailDeferred(RuntimeError):
    """后端暂不可用；保持资源 pending，不消耗重试次数。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "thumbnail_source_deferred",
        max_deferred_attempts: int | None = None,
        deferred_backoff_base_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.max_deferred_attempts = max_deferred_attempts
        self.deferred_backoff_base_seconds = deferred_backoff_base_seconds


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
