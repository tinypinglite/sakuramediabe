"""可视化媒体导入协议层。

覆盖目录浏览、导入触发、导入作业查询，以及失败文件的删除/重命名/重导请求与响应。
"""

from datetime import datetime
from typing import List, Literal, Optional

from src.schema.common.base import SchemaModel


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
    imported_count: int
    skipped_count: int
    failed_count: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, job) -> "ImportJobListItemResource":
        return cls.model_validate(
            {
                "id": job.id,
                "source_path": job.source_path,
                "library_id": job.library_id,
                "download_task_id": job.download_task_id,
                "task_run_id": job.task_run_id,
                "state": job.state,
                "imported_count": job.imported_count,
                "skipped_count": job.skipped_count,
                "failed_count": job.failed_count,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
            }
        )


class ImportJobResource(ImportJobListItemResource):
    failed_files: List[FailedFileResource] = []

    @classmethod
    def from_model(cls, job, *, failed_files: List[FailedFileResource]) -> "ImportJobResource":
        base = ImportJobListItemResource.from_model(job)
        return cls.model_validate({**base.model_dump(), "failed_files": failed_files})
