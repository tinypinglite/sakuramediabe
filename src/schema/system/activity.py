from datetime import datetime
from typing import Any

from pydantic import model_validator

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
    # 由 result_summary 的 failed 计数推导，不落库。
    has_warning: bool = False
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskRunAttentionResource(SchemaModel):
    task_key: str
    task_name: str
    latest_run: TaskRunResource
    failed_count_7d: int
    last_issue_at: datetime


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


class NotificationUpdateRequest(SchemaModel):
    # 批量设置已读状态：ids 指定目标通知，或 all=true 全量，二者互斥。
    ids: list[int] | None = None
    all: bool = False
    is_read: bool

    @model_validator(mode="after")
    def validate_target(self) -> "NotificationUpdateRequest":
        if self.all == bool(self.ids):
            raise ValueError("必须提供 ids 或 all=true，且二者不可同时提供")
        return self


class NotificationUpdateResponse(SchemaModel):
    # 状态更新结果：本次实际变更条数，以及操作后剩余未读总数。
    updated_count: int
    unread_count: int


class ActivityBootstrapResource(SchemaModel):
    notifications: PageResponse[NotificationResource]
    unread_count: int
    active_task_runs: list[TaskRunResource]
    task_runs: PageResponse[TaskRunResource]
