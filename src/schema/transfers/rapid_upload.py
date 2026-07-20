from datetime import datetime

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel


class MediaRapidUploadCreateRequest(SchemaModel):
    media_ids: list[int] = Field(min_length=1, max_length=1000)
    target_library_id: int = Field(gt=0)

    @field_validator("media_ids")
    @classmethod
    def validate_media_ids(cls, value: list[int]) -> list[int]:
        if any(media_id <= 0 for media_id in value):
            raise ValueError("media_ids must contain positive integers")
        # 重复选择没有业务意义，直接拒绝以免前端误判批次数量。
        if len(value) != len(set(value)):
            raise ValueError("media_ids must be unique")
        return value


class MediaRapidUploadTriggerResponse(SchemaModel):
    rapid_upload_batch_id: int
    task_run_id: int
    status: str


class MediaRapidUploadItemResource(SchemaModel):
    id: int
    media_id: int | None = None
    action: str
    state: str
    source_path: str
    source_size_bytes: int
    source_sha1: str | None = None
    target_fid: str | None = None
    target_pickcode: str | None = None
    target_name: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, item) -> "MediaRapidUploadItemResource":
        return cls.from_attributes_model(item)


class MediaRapidUploadBatchListItemResource(SchemaModel):
    id: int
    target_library_id: int
    retry_of_batch_id: int | None = None
    task_run_id: int | None = None
    completion_notification_id: int | None = None
    state: str
    total_count: int
    succeeded_count: int
    failed_count: int
    cleanup_failed_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, batch) -> "MediaRapidUploadBatchListItemResource":
        return cls.from_attributes_model(batch)


class MediaRapidUploadBatchResource(MediaRapidUploadBatchListItemResource):
    items: list[MediaRapidUploadItemResource]

    @classmethod
    def from_model(cls, batch, *, items) -> "MediaRapidUploadBatchResource":
        payload = MediaRapidUploadBatchListItemResource.from_model(batch).model_dump()
        payload["items"] = [
            MediaRapidUploadItemResource.from_model(item).model_dump()
            for item in items
        ]
        return cls.model_validate(payload)
