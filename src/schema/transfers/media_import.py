"""Provider-owned opaque source browse and import requests."""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from src.schema.common.base import SchemaModel


class ImportBrowseRequest(SchemaModel):
    library_id: int = Field(gt=0)
    parent_ref: dict[str, Any] | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ImportBrowseEntryResource(SchemaModel):
    source_ref: dict[str, Any]
    name: str
    entry_type: Literal["file", "directory"]
    size_bytes: int | None = None
    modified_at: datetime | None = None
    is_video: bool


class ImportBrowseResponse(SchemaModel):
    library_id: int
    entries: list[ImportBrowseEntryResource]
    next_cursor: str | None = None


class ImportRequest(SchemaModel):
    """JAV / 普通视频导入请求；source_ref 只由其 provider 解释。"""

    media_kind: Literal["jav", "video"]
    library_id: int = Field(gt=0)
    source_ref: dict[str, Any]
    source_disposition: Literal["keep", "delete_after_commit"] = "keep"
    collection_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_combination(self) -> "ImportRequest":
        if self.media_kind == "jav" and self.collection_id is not None:
            raise ValueError("jav import does not support collection_id")
        return self


class ImportResult(SchemaModel):
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    new_playable_movies: list[dict[str, object]] = Field(default_factory=list)
    created_video_ids: list[int] = Field(default_factory=list)


class ImportAcceptedResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
