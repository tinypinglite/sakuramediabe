from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.model import Media


class ThumbnailDeferred(RuntimeError):
    """后端暂不可用；保持资源 pending，不消耗重试次数。"""


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
