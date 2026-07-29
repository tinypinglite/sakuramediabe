from datetime import datetime

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel


class TaskRecordStateCountsResource(SchemaModel):
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    # kernel 记账任务（Wave 2 起）的失败二分：可重试（等 next_retry_at 到期）与确定性终态。
    failed_retryable: int = 0
    failed_terminal: int = 0
    # 已达到该任务的自动重试上限，不再自动排入；只能由用户显式重置后重新参与。
    exhausted: int = 0


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
    # 后端按投影状态计算的可用操作（Wave 4 统一 action 协议），前端只按枚举渲染。
    available_actions: list[str] = []


class MediaThumbnailTaskBatchResetRequest(SchemaModel):
    resource_ids: list[int] = Field(min_length=1, max_length=200)

    @field_validator("resource_ids")
    @classmethod
    def validate_resource_ids(cls, value: list[int]) -> list[int]:
        # 批量重置只接受唯一的正整数主键，避免重复项被误计入重置数量。
        if any(resource_id <= 0 for resource_id in value):
            raise ValueError("resource_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("resource_ids must be unique")
        return value


class MediaThumbnailTaskResetSkippedItem(SchemaModel):
    resource_id: int
    reason: str


class MediaThumbnailTaskBatchResetResponse(SchemaModel):
    task_key: str
    state: str
    reset_count: int
    resource_ids: list[int]
    skipped_count: int = 0
    skipped: list[MediaThumbnailTaskResetSkippedItem] = Field(default_factory=list)


class ResourceTaskActionRequest(SchemaModel):
    task_key: str
    action: str
    resource_ids: list[int]


class ResourceTaskActionSkippedItem(SchemaModel):
    resource_id: int
    reason: str


class ResourceTaskActionResponse(SchemaModel):
    task_key: str
    action: str
    # retry_now / rerun 会入队一个带 only_ids 的可跟踪 run；reset_retry_budget 为 null。
    task_run_id: int | None = None
    accepted_resource_ids: list[int] = []
    skipped: list[ResourceTaskActionSkippedItem] = []
