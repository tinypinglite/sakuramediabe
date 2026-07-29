"""统一资源任务操作协议（任务架构 Wave 4，见 docs/development/task-architecture.md）。

`POST /system/resource-task-actions`：前端不再按 task_key 硬编码按钮——
`available_actions` 由后端按投影状态计算，action 枚举值即请求参数。

- ``retry_now``：failed_retryable / exhausted → 立即重试（exhausted 隐式重开预算），
  置 next_retry_at 到期后入队一个带 only_ids 的可跟踪 run；
- ``rerun``：任意终态（succeeded / failed_terminal / exhausted / failed_retryable）
  强制重跑：投影回 pending（重开预算）后入队 only_ids run；
- ``reset_retry_budget``：exhausted / failed_* → 回 pending 重开预算，不建 run，
  等下一轮定时任务自然处理。

批量操作允许部分成功，逐条返回跳过原因；重复点击靠 action 级 mutex 去重（409）。
"""

from __future__ import annotations

from dataclasses import dataclass

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, ResourceTaskState
from src.model.base import get_database
from src.service.system.resource_task_actions_registry import (
    ACTION_ELIGIBLE_STATES,
    ACTION_RERUN,
    ACTION_RESET_RETRY_BUDGET,
    ACTION_RETRY_NOW,
    SUPPORTED_ACTIONS,
)
from src.service.system.resource_task_runner import (
    STATE_EXHAUSTED,
    STATE_FAILED_RETRYABLE,
    STATE_PENDING,
)
from src.service.system.resource_task_state_service import ResourceTaskStateService

SKIP_REASON_STATE_NOT_FOUND = "task_state_not_found"
SKIP_REASON_NOT_ACTIONABLE = "state_not_actionable"


@dataclass(frozen=True)
class ResourceTaskActionOutcome:
    task_key: str
    action: str
    task_run_id: int | None
    accepted_resource_ids: list[int]
    skipped: list[dict]


class ResourceTaskActionService:
    @classmethod
    def apply(
        cls, *, task_key: str, action: str, resource_ids: list[int]
    ) -> ResourceTaskActionOutcome:
        if action not in SUPPORTED_ACTIONS:
            raise ApiError(
                422,
                "unsupported_resource_task_action",
                "不支持的资源任务操作",
                {"action": action, "supported": list(SUPPORTED_ACTIONS)},
            )
        try:
            definition = ResourceTaskStateService.get_definition(task_key)
        except ValueError as exc:
            raise ApiError(
                404, "resource_task_not_registered", "资源任务不存在", {"task_key": task_key}
            ) from exc
        normalized_ids = [int(resource_id) for resource_id in resource_ids]
        if not normalized_ids:
            raise ApiError(
                422, "empty_resource_ids", "resource_ids 不能为空", {"task_key": task_key}
            )

        eligible_states = ACTION_ELIGIBLE_STATES[action]
        accepted: list[int] = []
        skipped: list[dict] = []
        now = utc_now_for_db()
        with get_database().atomic():
            records = {
                record.resource_id: record
                for record in ResourceTaskState.select()
                .where(
                    ResourceTaskState.task_key == definition.task_key,
                    ResourceTaskState.resource_type == definition.resource_type,
                    ResourceTaskState.resource_id.in_(normalized_ids),
                )
                .for_update()
            }
            for resource_id in normalized_ids:
                record = records.get(resource_id)
                if record is None:
                    skipped.append(
                        {"resource_id": resource_id, "reason": SKIP_REASON_STATE_NOT_FOUND}
                    )
                    continue
                if record.state not in eligible_states:
                    skipped.append(
                        {"resource_id": resource_id, "reason": SKIP_REASON_NOT_ACTIONABLE}
                    )
                    continue
                cls._transition(record, action=action, now=now)
                accepted.append(resource_id)

        task_run_id = None
        if accepted and action in (ACTION_RETRY_NOW, ACTION_RERUN):
            task_run_id = cls._enqueue_subset_run(
                task_key=definition.task_key, action=action, resource_ids=accepted
            )
        return ResourceTaskActionOutcome(
            task_key=definition.task_key,
            action=action,
            task_run_id=task_run_id,
            accepted_resource_ids=accepted,
            skipped=skipped,
        )

    @classmethod
    def _transition(cls, record: ResourceTaskState, *, action: str, now) -> None:
        """按 action 预置投影状态，让随后的 only_ids run 能按候选条件接住这些资源。

        历史不清：尝试链保留在 attempt 表，这里只重开预算 / 归位投影快照。
        """
        if action == ACTION_RETRY_NOW:
            if record.state == STATE_EXHAUSTED:
                record.retry_round = (record.retry_round or 0) + 1
                record.attempt_count = 0
            record.state = STATE_FAILED_RETRYABLE
            record.next_retry_at = now
        else:  # rerun / reset_retry_budget：回 pending 并重开预算
            record.state = STATE_PENDING
            record.retry_round = (record.retry_round or 0) + 1
            record.attempt_count = 0
            record.next_retry_at = None
            record.error_code = None
            record.last_error = None
            record.last_error_at = None
        record.last_trigger_type = "manual"
        record.updated_at = now
        record.save()

    @classmethod
    def _enqueue_subset_run(
        cls, *, task_key: str, action: str, resource_ids: list[int]
    ) -> int:
        from src.service.system.activity_service import ActivityService

        try:
            task_run = ActivityService.create_task_run(
                task_key=task_key,
                task_name=f"资源操作 {action}（{len(resource_ids)} 项）",
                trigger_type="manual",
                # action 级互斥：同任务的批量操作一次一个，连点返回 409。
                mutex_key=f"resource_action:{task_key}",
                params={"only_ids": resource_ids},
                scheduled_at=utc_now_for_db(),
            )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(
                f"resource_action:{task_key}"
            )
            raise ApiError(
                409,
                "resource_task_action_conflict",
                "该任务已有批量操作在排队或执行中",
                {
                    "task_key": task_key,
                    "blocking_task_run_id": blocking.id if blocking else None,
                },
            ) from exc
        return task_run.id
