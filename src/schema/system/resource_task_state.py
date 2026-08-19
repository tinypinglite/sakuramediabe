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
    # 该任务开放的统一 action 集合（rerun 只有域语义安全的任务声明），前端按此渲染批量操作入口。
    supported_actions: list[str] = []
    state_counts: TaskRecordStateCountsResource


class ResourceTaskRecordResource(SchemaModel):
    task_key: str
    resource_type: str
    resource_id: int
    state: str
    attempt_count: int = 0
    # 延后状态复用 task_state.extra 持久化，不占失败预算。
    deferred_count: int = 0
    deferred_limit: int = 0
    deferred_reason: str | None = None
    next_retry_at: datetime | None = None
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


class ResourceTaskActionRequest(SchemaModel):
    task_key: str
    action: str
    # 显式指定操作目标；缺省（None / 空列表）时按 state 圈定整批，仅 reset_retry_budget
    # 支持（retry_now / rerun 会把 ids 写进 run params，必须显式指定并限制规模）。
    resource_ids: list[int] | None = Field(default=None, max_length=500)
    # 批量圈定的状态筛选：failed_retryable / failed_terminal / exhausted；缺省为三者全部。
    state: str | None = None

    @field_validator("resource_ids")
    @classmethod
    def validate_resource_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        # 只接受唯一的正整数主键，避免重复项被误计入操作数量。
        if any(resource_id <= 0 for resource_id in value):
            raise ValueError("resource_ids must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("resource_ids must be unique")
        return value


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
