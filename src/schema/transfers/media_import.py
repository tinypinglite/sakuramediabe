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
    # 失败文件随任务结果保存，供用户查看和发起人工元数据匹配后的重试。
    # source_ref 等宿主内部字段只留在 summary 中，失败项资源模型不暴露。
    failed_files: list[dict[str, Any]] = Field(default_factory=list)


class ImportFailedItemResource(SchemaModel):
    id: str
    relative_path: str
    size_bytes: int
    is_video: bool
    reason: str
    detail: str = ""
    kind: str
    state: Literal["pending", "queued", "resolved"] = "pending"
    retry_task_run_id: int | None = None
    resolved_movie_id: int | None = None
    resolved_media_id: int | None = None
    last_retry_error: str | None = None
    can_manual_search: bool = False


class ImportMetadataCandidateResource(SchemaModel):
    candidate_id: str
    source: Literal["javdb", "plugin"]
    source_name: str
    source_id: str | None = None
    javdb_id: str | None = None
    movie_number: str
    title: str
    cover_url: str | None = None
    release_date: str | None = None
    duration_minutes: int


class ImportMetadataSourceErrorResource(SchemaModel):
    source: str
    source_name: str
    reason: str
    detail: str


class ImportMetadataSearchRequest(SchemaModel):
    movie_number: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_movie_number(self) -> "ImportMetadataSearchRequest":
        self.movie_number = self.movie_number.strip()
        if not self.movie_number:
            raise ValueError("movie_number cannot be blank")
        return self


class ImportMetadataSearchResponse(SchemaModel):
    movie_number: str
    candidates: list[ImportMetadataCandidateResource] = Field(default_factory=list)
    source_errors: list[ImportMetadataSourceErrorResource] = Field(default_factory=list)


class ImportFailedItemRetryRequest(SchemaModel):
    candidate_id: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_candidate_id(self) -> "ImportFailedItemRetryRequest":
        self.candidate_id = self.candidate_id.strip()
        if not self.candidate_id:
            raise ValueError("candidate_id cannot be blank")
        return self


class ImportAcceptedResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
