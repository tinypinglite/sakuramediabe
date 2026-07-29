"""统一资源任务操作协议（任务架构 Wave 4，见 docs/development/task-architecture.md）。

`POST /system/resource-task-actions`：资源级操作的唯一入口——前端不再按 task_key
硬编码按钮，`available_actions` 由后端按"任务声明支持集 ∩ 投影状态"计算返回。

- ``retry_now``：failed_retryable / exhausted → 立即重试（exhausted 隐式重开预算），
  置 next_retry_at 到期后入队一个带 only_ids 的可跟踪 run；
- ``rerun``：通用"立即强制执行"入口，running 之外的任意状态可用；从未记账的资源
  就地播种状态行。only_ids 子集运行按约定绕过"已完成"域条件（手动指定即强制），
  因此 rerun 只对域语义安全的任务开放（definition.supported_actions 声明）；
- ``reset_retry_budget``：exhausted / failed_* → 回 pending 重开预算，不建 run，
  等下一轮定时任务自然处理。``resource_ids`` 缺省时按 ``state`` 圈定整批重置
  （只有本 action 支持批量圈定：不入队 run，没有 params 膨胀问题）。

批量操作允许部分成功，逐条返回跳过原因；重复点击靠 action 级 mutex 去重（409）：
单资源操作互斥到资源粒度（影片页连点去重、不同影片互不阻塞），多资源操作互斥到任务粒度。
"""

from __future__ import annotations

from dataclasses import dataclass

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import ResourceTaskState
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
from src.service.system.resource_task_state_service import (
    ResourceTaskDefinition,
    ResourceTaskStateService,
)

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
        cls,
        *,
        task_key: str,
        action: str,
        resource_ids: list[int] | None = None,
        state: str | None = None,
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
        if action not in definition.supported_actions:
            raise ApiError(
                422,
                "resource_task_action_not_supported",
                "该任务不支持此操作",
                {
                    "task_key": definition.task_key,
                    "action": action,
                    "supported": list(definition.supported_actions),
                },
            )

        normalized_ids = [int(resource_id) for resource_id in (resource_ids or [])]
        if not normalized_ids:
            # 缺省 resource_ids = 按状态圈定整批；只有 reset_retry_budget 支持
            # （不入队 run、无 params 膨胀），且用集合 UPDATE 避免大事务逐行往返。
            return cls._apply_bulk_reset(definition=definition, action=action, state=state)

        # 领域合格性前置：影片缺必填字段 / 媒体失效 / 未订阅等，逐条落跳过原因。
        ineligible: dict[int, str] = {}
        if definition.check_actionable is not None:
            ineligible = definition.check_actionable(normalized_ids)

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
                reason = ineligible.get(resource_id)
                if reason is not None:
                    skipped.append({"resource_id": resource_id, "reason": reason})
                    continue
                record = records.get(resource_id)
                if record is None:
                    if action != ACTION_RERUN:
                        # retry_now / reset 只对已有失败记录有意义；rerun 是通用触发
                        # 入口，从未记账的资源就地播种（与内核 begin_attempt 的
                        # get_or_create 同语义）。
                        skipped.append(
                            {"resource_id": resource_id, "reason": SKIP_REASON_STATE_NOT_FOUND}
                        )
                        continue
                    record = cls._seed_record(definition, resource_id)
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
    def _apply_bulk_reset(
        cls, *, definition: ResourceTaskDefinition, action: str, state: str | None
    ) -> ResourceTaskActionOutcome:
        """按状态圈定整批 reset_retry_budget：一条集合 UPDATE + RETURNING 收口。"""
        if action != ACTION_RESET_RETRY_BUDGET:
            raise ApiError(
                422,
                "empty_resource_ids",
                "retry_now / rerun 必须显式指定 resource_ids",
                {"task_key": definition.task_key, "action": action},
            )
        eligible_states = ACTION_ELIGIBLE_STATES[action]
        normalized_state = str(state or "").strip().lower()
        if normalized_state:
            if normalized_state not in eligible_states:
                raise ApiError(
                    422,
                    "invalid_resource_task_action_state",
                    "该状态不支持批量重置",
                    {
                        "task_key": definition.task_key,
                        "state": normalized_state,
                        "supported": sorted(eligible_states),
                    },
                )
            target_states = (normalized_state,)
        else:
            target_states = tuple(sorted(eligible_states))

        scope_condition = (
            (ResourceTaskState.task_key == definition.task_key)
            & (ResourceTaskState.resource_type == definition.resource_type)
            & (ResourceTaskState.state.in_(target_states))
        )
        candidate_ids = [
            row[0]
            for row in ResourceTaskState.select(ResourceTaskState.resource_id)
            .where(scope_condition)
            .order_by(ResourceTaskState.resource_id.asc())
            .tuples()
        ]
        skipped: list[dict] = []
        if candidate_ids and definition.check_actionable is not None:
            ineligible = definition.check_actionable(candidate_ids)
            skipped = [
                {"resource_id": resource_id, "reason": reason}
                for resource_id, reason in sorted(ineligible.items())
            ]
            candidate_ids = [
                resource_id for resource_id in candidate_ids if resource_id not in ineligible
            ]
        accepted: list[int] = []
        if candidate_ids:
            now = utc_now_for_db()
            # where 里复核 state：圈定与执行之间被 worker 领走（running）的行自然落选。
            update_query = (
                ResourceTaskState.update(
                    state=STATE_PENDING,
                    retry_round=ResourceTaskState.retry_round + 1,
                    attempt_count=0,
                    next_retry_at=None,
                    error_code=None,
                    last_error=None,
                    last_error_at=None,
                    last_trigger_type="manual",
                    updated_at=now,
                )
                .where(
                    scope_condition,
                    ResourceTaskState.resource_id.in_(candidate_ids),
                )
                .returning(ResourceTaskState.resource_id)
            )
            accepted = sorted(row.resource_id for row in update_query.execute())
        return ResourceTaskActionOutcome(
            task_key=definition.task_key,
            action=action,
            task_run_id=None,
            accepted_resource_ids=accepted,
            skipped=skipped,
        )

    @classmethod
    def _seed_record(
        cls, definition: ResourceTaskDefinition, resource_id: int
    ) -> ResourceTaskState:
        """rerun 对从未记账的资源播种 pending 行；并发建行冲突回退到锁行重读。"""
        try:
            with get_database().atomic():  # savepoint：唯一索引冲突只回滚这一条
                return ResourceTaskState.create(
                    task_key=definition.task_key,
                    resource_type=definition.resource_type,
                    resource_id=int(resource_id),
                    state=STATE_PENDING,
                )
        except IntegrityError:
            return (
                ResourceTaskState.select()
                .where(
                    ResourceTaskState.task_key == definition.task_key,
                    ResourceTaskState.resource_type == definition.resource_type,
                    ResourceTaskState.resource_id == int(resource_id),
                )
                .for_update()
                .get()
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

        # 单资源操作互斥到资源粒度：影片页对不同影片连续触发互不阻塞，
        # 同一影片连点去重。多资源批量仍是任务粒度一次一个。
        if len(resource_ids) == 1:
            mutex_key = f"resource_action:{task_key}:{resource_ids[0]}"
        else:
            mutex_key = f"resource_action:{task_key}"
        try:
            task_run = ActivityService.create_task_run(
                task_key=task_key,
                task_name=f"资源操作 {action}（{len(resource_ids)} 项）",
                trigger_type="manual",
                mutex_key=mutex_key,
                params={"only_ids": resource_ids},
                scheduled_at=utc_now_for_db(),
            )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "resource_task_action_conflict",
                "相同目标的操作已在排队或执行中",
                {
                    "task_key": task_key,
                    "blocking_task_run_id": blocking.id if blocking else None,
                },
            ) from exc
        return task_run.id
