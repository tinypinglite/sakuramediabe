from datetime import datetime
from typing import List, Literal, Optional

from pydantic import Field, computed_field, field_validator

# 失败条目结构与中文状态说明复用 transfers 域既有定义，避免平行再造 DTO。
from src.common.media_import_status import describe_import_job_state
from src.schema.common.base import SchemaModel
from src.schema.transfers.media_import import FailedFileResource


class VideoImportRequest(SchemaModel):
    # 指定一个目录或单个视频文件导入；library 必填，可选地一并关联合集。
    source_path: str = Field(min_length=1)
    library_id: int
    transfer_mode: Literal["auto", "cleanup-source"] = "auto"
    collection_id: int | None = None

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_path cannot be blank")
        return normalized


class VideoImportTriggerResponse(SchemaModel):
    video_import_job_id: int
    task_run_id: int
    status: str


class VideoImportJobListItemResource(SchemaModel):
    id: int
    source_path: str
    library_id: int
    task_run_id: Optional[int] = None
    collection_id: Optional[int] = None
    state: str
    transfer_mode: str = "auto"
    imported_count: int
    skipped_count: int
    failed_count: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def state_label(self) -> str:
        # 导入作业状态的中文说明（与 JAV 导入共用一套状态枚举）。
        return describe_import_job_state(self.state)

    @classmethod
    def from_model(cls, job) -> "VideoImportJobListItemResource":
        return cls.from_attributes_model(job)


class VideoImportJobResource(VideoImportJobListItemResource):
    failed_files: List[FailedFileResource] = []

    @classmethod
    def from_model(cls, job, *, failed_files: List[FailedFileResource]) -> "VideoImportJobResource":
        payload = VideoImportJobListItemResource.from_attributes_model(job).model_dump()
        payload["failed_files"] = [item.model_dump() for item in failed_files]
        return cls.model_validate(payload)
