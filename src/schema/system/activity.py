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
    event_type: str | None = None
    dedupe_key: str | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    is_read: bool
    created_at: datetime
    updated_at: datetime
    related_task_run_id: int | None = None
    related_resource_type: str | None = None
    related_resource_id: int | None = None


class NotificationReadResponse(SchemaModel):
    id: int
    is_read: bool
    read_at: datetime | None = None


class NotificationReadBatchRequest(SchemaModel):
    # 批量标记已读的目标通知 ID 列表。
    ids: list[int]


class NotificationBatchReadResponse(SchemaModel):
    # 标记已读结果：本次新置为已读的条数，以及操作后剩余未读总数。
    # 按 ID 批量（read）与全部已读（read-all）共用此响应。
    updated_count: int
    unread_count: int


class ActivityBootstrapResource(SchemaModel):
    notifications: PageResponse[NotificationResource]
    unread_count: int
    active_task_runs: list[TaskRunResource]
    task_runs: PageResponse[TaskRunResource]
