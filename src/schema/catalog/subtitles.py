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
