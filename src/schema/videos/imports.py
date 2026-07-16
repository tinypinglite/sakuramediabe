from datetime import datetime
from typing import List, Literal, Optional

from pydantic import computed_field, field_validator, model_validator

# 失败条目结构与中文状态说明复用 transfers 域既有定义，避免平行再造 DTO。
from src.common.media_import_status import describe_import_job_state
from src.schema.common.base import SchemaModel
from src.schema.transfers.media_import import FailedFileResource


class VideoImportRequest(SchemaModel):
    # 本地路径、115 目录 CID、115 文件 FID 恰好提供一个；library 必填，可选地一并关联合集。
    source_path: str | None = None
    source_cid: str | None = None
    source_fid: str | None = None
    library_id: int
    transfer_mode: Literal["auto", "copy", "cleanup-source"] | None = None
    collection_id: int | None = None

    @field_validator("source_path", "source_cid", "source_fid")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("import source cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_exactly_one_source(self) -> "VideoImportRequest":
        if sum(value is not None for value in (self.source_path, self.source_cid, self.source_fid)) != 1:
            raise ValueError("exactly one of source_path / source_cid / source_fid is required")
        return self


class VideoImportTriggerResponse(SchemaModel):
    video_import_job_id: int
    task_run_id: int
    status: str


class VideoImportJobListItemResource(SchemaModel):
    id: int
    source_path: str
    source_cid: Optional[str] = None
    source_fid: Optional[str] = None
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
