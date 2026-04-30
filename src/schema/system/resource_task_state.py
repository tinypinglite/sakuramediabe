from datetime import datetime

from src.schema.common.base import SchemaModel


class TaskRecordStateCountsResource(SchemaModel):
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0


class TaskRecordResourceSummary(SchemaModel):
    resource_id: int
    movie_number: str | None = None
    title: str | None = None
    path: str | None = None
    valid: bool | None = None


class ResourceTaskDefinitionResource(SchemaModel):
    task_key: str
    resource_type: str
    display_name: str
    default_sort: str
    state_counts: TaskRecordStateCountsResource


class ResourceTaskRecordResource(SchemaModel):
    task_key: str
    resource_type: str
    resource_id: int
    state: str
    attempt_count: int = 0
    last_attempted_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_task_run_id: int | None = None
    last_trigger_type: str | None = None
    created_at: datetime
    updated_at: datetime
    resource: TaskRecordResourceSummary | None = None
