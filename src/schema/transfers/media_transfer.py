"""Requests and task results for moving managed media between libraries."""

from typing import Any

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from src.schema.common.base import SchemaModel


class MediaStorageTransferSelectionRequest(SchemaModel):
    media_ids: list[int] = Field(min_length=1, max_length=1000)

    @field_validator("media_ids", mode="before")
    @classmethod
    def reject_boolean_media_ids(cls, value):
        if isinstance(value, (list, tuple)) and any(
            isinstance(item, bool) for item in value
        ):
            raise ValueError("media_ids 必须全部为正整数")
        return value

    @field_validator("media_ids")
    @classmethod
    def validate_media_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("media_ids 必须全部为正整数")
        if len(set(value)) != len(value):
            raise ValueError("media_ids 不可重复")
        return value


class MediaStorageTransferCandidatesRequest(MediaStorageTransferSelectionRequest):
    pass


class MediaStorageTransferRequest(MediaStorageTransferSelectionRequest):
    target_library_id: int = Field(gt=0)

    @field_validator("target_library_id", mode="before")
    @classmethod
    def reject_boolean_target_library_id(cls, value):
        if isinstance(value, bool):
            raise PydanticCustomError(
                "positive_integer",
                "target_library_id 必须为正整数",
            )
        return value


class MediaStorageTransferLibraryResource(SchemaModel):
    id: int
    name: str


class MediaStorageTransferCandidatesResponse(SchemaModel):
    source_library: MediaStorageTransferLibraryResource
    targets: list[MediaStorageTransferLibraryResource] = Field(default_factory=list)


class MediaStorageTransferAcceptedResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str


class MediaStorageTransferResult(SchemaModel):
    transferred_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    cleanup_incomplete_count: int = 0
    unexecuted_media_ids: list[int] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    reason_code: str | None = None
