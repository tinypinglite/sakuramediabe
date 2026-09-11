from typing import Any

from pydantic import Field

from src.schema.common.base import SchemaModel


class MediaSummaryResource(SchemaModel):
    media_id: int = Field(validation_alias="id")
    library_id: int | None = None
    library_name: str | None = None
    provider_key: str | None = None
    file_name: str = ""
    resolution: str | None = None
    file_size_bytes: int = 0
    duration_seconds: int = 0
    video_info: dict[str, Any] | None = None
    valid: bool = True
