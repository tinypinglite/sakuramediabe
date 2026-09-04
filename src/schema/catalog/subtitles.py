from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pydantic import Field

from src.schema.common.base import SchemaModel


class MovieSubtitleItemResource(SchemaModel):
    subtitle_id: int = Field(validation_alias="id")
    url: str
    created_at: datetime
    file_name: str


class MovieSubtitleListResource(SchemaModel):
    movie_number: str
    items: list[MovieSubtitleItemResource]


class SubtitleImportStatus(str, Enum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    MOVIE_NOT_FOUND = "movie_not_found"
    INVALID_FORMAT = "invalid_format"


class SubtitleImportResult(SchemaModel):
    """插件/资产 API 写入字幕的幂等结果。"""

    status: SubtitleImportStatus
    subtitle_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SubtitleAsset:
    """字幕只读元信息，不暴露文件路径或 ORM 对象。"""

    subtitle_id: int
    file_name: str
    format: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class SubtitleContent:
    """原始字幕字节与本次读取内容的 SHA256。"""

    subtitle_id: int
    content: bytes
    sha256: str


class SubtitleReadError(ValueError):
    """字幕读取失败；插件通过 code 判断原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
