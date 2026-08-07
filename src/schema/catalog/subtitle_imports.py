"""手动字幕导入协议层。

字幕目录导入作业的触发、列表、详情与失败文件操作请求/响应。
失败条目结构复用 transfers 域既有 FailedFileResource，状态文案共用同一套状态枚举。
"""

from datetime import datetime

from pydantic import computed_field, field_validator

from src.common.media_import_status import describe_import_job_state
from src.schema.common.base import SchemaModel
from src.schema.transfers.media_import import FailedFileResource


class SubtitleImportCreateRequest(SchemaModel):
    source_path: str

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("source_path cannot be blank")
        return normalized


class SubtitleImportTriggerResponse(SchemaModel):
    subtitle_import_job_id: int
    task_run_id: int
    status: str


class SubtitleImportJobListItemResource(SchemaModel):
    id: int
    source_path: str
    task_run_id: int | None = None
    state: str
    transfer_mode: str = "auto"
    imported_count: int
    skipped_count: int
    failed_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def state_label(self) -> str:
        # 与 JAV/视频导入共用一套作业状态文案。
        return describe_import_job_state(self.state)

    @classmethod
    def from_model(cls, job) -> "SubtitleImportJobListItemResource":
        return cls.from_attributes_model(job)


class SubtitleImportJobResource(SubtitleImportJobListItemResource):
    failed_files: list[FailedFileResource] = []

    @classmethod
    def from_model(
        cls,
        job,
        *,
        failed_files: list[FailedFileResource],
    ) -> "SubtitleImportJobResource":
        payload = SubtitleImportJobListItemResource.from_attributes_model(job).model_dump()
        payload["failed_files"] = [item.model_dump() for item in failed_files]
        return cls.model_validate(payload)
