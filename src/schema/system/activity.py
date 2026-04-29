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
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


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


class NotificationArchiveResponse(SchemaModel):
    id: int
    archived: bool
    archived_at: datetime | None = None


class NotificationUnreadCountResource(SchemaModel):
    unread_count: int


class ActivityBootstrapResource(SchemaModel):
    latest_event_id: int
    notifications: PageResponse[NotificationResource]
    unread_count: int
    active_task_runs: list[TaskRunResource]
    task_runs: PageResponse[TaskRunResource]


class SystemEventEnvelope(SchemaModel):
    event_id: int
    event: str
    data: dict[str, Any]
