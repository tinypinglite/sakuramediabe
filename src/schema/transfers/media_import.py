"""可视化媒体导入协议层。

覆盖目录浏览、导入触发、导入作业查询，以及失败文件的删除/重命名/重导请求与响应。
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import computed_field

from src.schema.common.base import SchemaModel
# media_import_status 是零依赖的取值/中文说明集中模块，这里仅用于把原始状态码转换成展示文案。
from src.common.media_import_status import (
    describe_failed_file_kind,
    describe_failure_reason,
    describe_import_job_state,
)


class FilesystemEntryResource(SchemaModel):
    name: str
    path: str
    type: Literal["dir", "video", "file"]
    size: int
    is_video: bool


class FilesystemListResponse(SchemaModel):
    path: str
    parent: Optional[str] = None
    entries: List[FilesystemEntryResource]


class FailedFileResource(SchemaModel):
    path: str
    reason: str
    detail: str = ""
    # 条目分类：file=可重导/删除/重命名的单文件失败；skipped=主动跳过；warning=导入后告警；job=任务级失败（path 为目录）。
    kind: str = "file"

    @computed_field
    @property
    def reason_label(self) -> str:
        # 失败原因的中文说明，便于前端直接展示与排障。
        return describe_failure_reason(self.reason)

    @computed_field
    @property
    def kind_label(self) -> str:
        # 条目分类的中文说明。
        return describe_failed_file_kind(self.kind)


class ImportJobCreateRequest(SchemaModel):
    library_id: int
    source_path: str
    transfer_mode: Literal["auto", "cleanup-source"] = "auto"


class RetryFailedFilesRequest(SchemaModel):
    # 为空表示重导该作业全部失败文件，否则只重导交集中的文件。
    files: Optional[List[str]] = None


class DeleteFailedFileRequest(SchemaModel):
    path: str


class RenameFailedFileRequest(SchemaModel):
    path: str
    new_name: str


class ImportJobTriggerResponse(SchemaModel):
    import_job_id: int
    task_run_id: int
    status: str


class ImportJobListItemResource(SchemaModel):
    id: int
    source_path: str
    library_id: int
    download_task_id: Optional[int] = None
    task_run_id: Optional[int] = None
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
        # 导入作业状态的中文说明。
        return describe_import_job_state(self.state)

    @classmethod
    def from_model(cls, job) -> "ImportJobListItemResource":
        # Peewee FK 的 *_id 属性可直接按字段名读取，复用 SchemaModel 的属性转换。
        return cls.from_attributes_model(job)


class ImportJobResource(ImportJobListItemResource):
    failed_files: List[FailedFileResource] = []

    @classmethod
    def from_model(cls, job, *, failed_files: List[FailedFileResource]) -> "ImportJobResource":
        payload = ImportJobListItemResource.from_attributes_model(job).model_dump()
        payload["failed_files"] = [item.model_dump() for item in failed_files]
        return cls.model_validate(payload)
