from datetime import datetime
from typing import Any

from src.schema.common.base import SchemaModel
from src.schema.common.pagination import PageResponse


class TaskRunResource(SchemaModel):
    id: int
    task_key: str
    task_name: str
    trigger_type: str
    state: str
    progress_current: int | None = None
    progress_total: int | None = None
    progress_text: str | None = None
    result_text: str | None = None
    result_summary: dict[str, Any] | None = None
    error_message: str | None = None
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskRunItemResource(SchemaModel):
    id: int
    task_run_id: int
    item_type: str
    item_key: str
    state: str
    source_path: str | None = None
    target_path: str | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    related_resource_type: str | None = None
    related_resource_id: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    extra: dict | list | None = None
    created_at: datetime
    updated_at: datetime


class TaskRunCancelResponse(SchemaModel):
    id: int
    state: str
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = None


class NotificationResource(SchemaModel):
    id: int
    category: str
    title: str
    content: str
    is_read: bool
    archived: bool
    created_at: datetime
    updated_at: datetime
    related_task_run_id: int | None = None
    related_resource_type: str | None = None
    related_resource_id: int | None = None


class NotificationReadResponse(SchemaModel):
    id: int
    is_read: bool
    read_at: datetime | None = None


class ActivityBootstrapResource(SchemaModel):
    latest_event_id: int
    notifications: PageResponse[NotificationResource]
    unread_count: int
    active_task_runs: list[TaskRunResource]
    task_runs: PageResponse[TaskRunResource]


class DirectoryEntryResource(SchemaModel):
    name: str
    path: str
    readable: bool
    selectable: bool
    is_symlink: bool


class DirectoryListResource(SchemaModel):
    path: str
    parent_path: str | None = None
    entries: list[DirectoryEntryResource]


class ManualMediaImportRequest(SchemaModel):
    library_id: int
    source_path: str


class ManualMediaImportResponse(SchemaModel):
    task_run_id: int
    import_job_id: int
    status: str


class MediaImportHistoryResource(SchemaModel):
    import_job_id: int
    task_run_id: int | None = None
    library_id: int
    source_path: str
    state: str
    imported_count: int
    skipped_count: int
    failed_count: int
    task_state: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_text: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SystemEventEnvelope(SchemaModel):
    event_id: int
    event: str
    data: dict[str, Any]
