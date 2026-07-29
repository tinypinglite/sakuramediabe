"""资源任务执行内核（任务架构 Wave 2，见 docs/development/task-architecture.md）。

统一逐资源生命周期记账：候选 = 域条件 ∩ 状态条件；每个资源固定走
begin_attempt → process_one → (succeeded / failed_retryable / failed_terminal /
exhausted / deferred / aborted)，attempt 历史只增不改，投影同事务更新。

状态词汇（仅已迁到本内核的任务使用；未迁任务仍是 failed + extra.terminal）：
- ``failed_retryable``：可重试失败，``next_retry_at`` 决定下次进候选的时间；
- ``failed_terminal``：确定性失败，永不自动重试（取代 extra.terminal）；
- ``exhausted``：本轮预算耗尽，``reset_retry_budget`` 动作可重开（retry_round+1、
  attempt_count 归零；终身次数由 attempt 表行数体现）。

``deferred`` / ``aborted`` 不消耗预算：投影回退到尝试前的状态并回滚本轮计数，
attempt 行保留作为历史。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable

from loguru import logger
from peewee import fn

from src.common.runtime_time import utc_now_for_db
from src.model import ResourceTaskAttempt, ResourceTaskState
from src.model.base import get_database

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED_RETRYABLE = "failed_retryable"
STATE_FAILED_TERMINAL = "failed_terminal"
STATE_EXHAUSTED = "exhausted"

ERROR_CODE_UNHANDLED = "unhandled_exception"


class TaskItemError(Exception):
    """单资源处理失败：带结构化 error_code 与可重试性，由内核分类落库。"""

    def __init__(self, error_code: str, message: str | None = None, *, retryable: bool = True):
        super().__init__(message or error_code)
        self.error_code = error_code
        self.retryable = retryable


class TaskItemDeferred(Exception):
    """单资源本轮跳过（既不成功也不失败，不耗预算），如缩略图源暂不可用。"""

    def __init__(self, error_code: str, message: str | None = None):
        super().__init__(message or error_code)
        self.error_code = error_code


class TaskAbortError(Exception):
    """任务级中止（如上游熔断）：当前资源回 pending 不耗预算，整个 run 中断。"""

    def __init__(self, error_code: str, message: str | None = None):
        super().__init__(message or error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class RetryPolicy:
    """每轮重试预算与退避。exempt 按资源豁免预算（如订阅搜索的新片不计次）。"""

    max_attempts: int
    backoff_base_seconds: int
    backoff_factor: float = 2.0
    backoff_max_seconds: int = 86400
    exempt: Callable[[Any], bool] | None = None

    def next_retry_delay_seconds(self, round_attempt_count: int) -> int:
        exponent = max(round_attempt_count - 1, 0)
        delay = self.backoff_base_seconds * (self.backoff_factor ** exponent)
        return int(min(delay, self.backoff_max_seconds))


@dataclass
class RunContext:
    task_key: str
    reporter: Any
    trigger_type: str | None = None
    task_run_id: int | None = None
    shared: Any = None


@dataclass(frozen=True)
class ResourceTaskSpec:
    task_key: str
    resource_type: str
    retry: RetryPolicy
    # 只写域条件（如 Movie.desc == ""）；内核把状态条件作为参数传入，由任务合入查询。
    select_candidates: Callable[..., Iterable[Any]]
    process_one: Callable[[RunContext, Any], Any]
    # 每 run 一次，产出跨资源共享上下文（provider 会话 / 预载缓存 / 熔断器等）。
    setup_run: Callable[[RunContext], Any] | None = None
    resource_id_of: Callable[[Any], int] = field(default=lambda resource: int(resource.id))
    # 任务内并发：>1 时内核开线程池逐资源执行（每 worker 自建 DB 连接、ctx 显式传参，
    # 取代旧的 ContextVar wrap）。并发模式下 TaskAbortError 在收齐已提交项后再抛出。
    concurrency: int = 1


class ResourceTaskLedger:
    """单资源尝试记账。独立于 Runner 暴露，单资源直跑路径（导入链路联动）复用。"""

    @classmethod
    def eligible_state_condition(cls, task_key: str, resource_type: str, resource_id_field):
        """内核侧状态条件：无状态行 / pending / 到期的 failed_retryable。"""
        current = utc_now_for_db()
        matching = ResourceTaskState.select(ResourceTaskState.id).where(
            ResourceTaskState.task_key == task_key,
            ResourceTaskState.resource_type == resource_type,
            ResourceTaskState.resource_id == resource_id_field,
        )
        return (
            ~fn.EXISTS(matching)
            | fn.EXISTS(matching.where(ResourceTaskState.state == STATE_PENDING))
            | fn.EXISTS(
                matching.where(
                    ResourceTaskState.state == STATE_FAILED_RETRYABLE,
                    (
                        ResourceTaskState.next_retry_at.is_null(True)
                        | (ResourceTaskState.next_retry_at <= current)
                    ),
                )
            )
        )

    @classmethod
    def begin_attempt(
        cls,
        *,
        task_key: str,
        resource_type: str,
        resource_id: int,
        trigger_type: str | None,
        task_run_id: int | None,
    ) -> tuple[ResourceTaskAttempt, ResourceTaskState, str]:
        """开启一次尝试：attempt 行 + 投影置 running；返回 (attempt, 投影, 尝试前状态)。"""
        now = utc_now_for_db()
        with get_database().atomic():
            record, _created = ResourceTaskState.get_or_create(
                task_key=task_key,
                resource_type=resource_type,
                resource_id=int(resource_id),
                defaults={"state": STATE_PENDING},
            )
            prior_state = record.state
            attempt = ResourceTaskAttempt.create(
                task_key=task_key,
                resource_type=resource_type,
                resource_id=int(resource_id),
                task_run=task_run_id,
                attempt_no=record.attempt_count + 1,
                trigger_type=trigger_type,
                state=STATE_RUNNING,
                started_at=now,
            )
            record.state = STATE_RUNNING
            record.attempt_count += 1
            record.last_attempted_at = now
            record.last_trigger_type = trigger_type
            record.last_task_run_id = task_run_id
            record.last_attempt = attempt
            record.updated_at = now
            record.save()
            return attempt, record, prior_state

    @classmethod
    def finish_success(cls, attempt: ResourceTaskAttempt, record: ResourceTaskState) -> None:
        now = utc_now_for_db()
        with get_database().atomic():
            cls._finalize_attempt(attempt, STATE_SUCCEEDED, finished_at=now)
            record.state = STATE_SUCCEEDED
            # 成功即收口本轮：计数归零，让周期性任务（如互动同步）的历史成功
            # 不吃掉失败预算；终身次数由 attempt 表行数体现。
            record.attempt_count = 0
            record.last_succeeded_at = now
            record.last_error = None
            record.last_error_at = None
            record.error_code = None
            record.next_retry_at = None
            record.updated_at = now
            record.save()

    @classmethod
    def finish_failure(
        cls,
        attempt: ResourceTaskAttempt,
        record: ResourceTaskState,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        policy: RetryPolicy,
        budget_exempt: bool = False,
    ) -> str:
        """按可重试性与预算分类落库，返回投影最终状态。"""
        now = utc_now_for_db()
        if not retryable:
            final_state = STATE_FAILED_TERMINAL
            next_retry_at = None
        elif not budget_exempt and record.attempt_count >= policy.max_attempts:
            final_state = STATE_EXHAUSTED
            next_retry_at = None
        else:
            final_state = STATE_FAILED_RETRYABLE
            next_retry_at = now + timedelta(
                seconds=policy.next_retry_delay_seconds(record.attempt_count)
            )
        with get_database().atomic():
            cls._finalize_attempt(
                attempt,
                "failed",
                finished_at=now,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
            )
            record.state = final_state
            record.last_error = error_message
            record.last_error_at = now
            record.error_code = error_code
            record.next_retry_at = next_retry_at
            record.updated_at = now
            record.save()
        return final_state

    @classmethod
    def finish_non_consuming(
        cls,
        attempt: ResourceTaskAttempt,
        record: ResourceTaskState,
        *,
        prior_state: str,
        attempt_state: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """deferred / aborted 收口：attempt 留档，投影回退原状态并回滚本轮计数。"""
        now = utc_now_for_db()
        with get_database().atomic():
            cls._finalize_attempt(
                attempt,
                attempt_state,
                finished_at=now,
                error_code=error_code,
                error_message=error_message,
            )
            record.state = prior_state if prior_state != STATE_RUNNING else STATE_PENDING
            record.attempt_count = max(record.attempt_count - 1, 0)
            record.updated_at = now
            record.save()

    @classmethod
    def recover_running(cls, task_key: str, *, error_message: str) -> int:
        """崩溃回收：running 投影 → failed_retryable（立即到期），attempt 收口 aborted。

        中断不消耗预算（回滚本轮计数），语义与 deferred/abort 一致。
        """
        now = utc_now_for_db()
        running_records = list(
            ResourceTaskState.select().where(
                ResourceTaskState.task_key == task_key,
                ResourceTaskState.state == STATE_RUNNING,
            )
        )
        for record in running_records:
            with get_database().atomic():
                if record.last_attempt_id is not None:
                    attempt = ResourceTaskAttempt.get_or_none(
                        ResourceTaskAttempt.id == record.last_attempt_id
                    )
                    if attempt is not None and attempt.state == STATE_RUNNING:
                        cls._finalize_attempt(
                            attempt,
                            "aborted",
                            finished_at=now,
                            error_code="interrupted",
                            error_message=error_message,
                        )
                record.state = STATE_FAILED_RETRYABLE
                record.attempt_count = max(record.attempt_count - 1, 0)
                record.last_error = error_message
                record.last_error_at = now
                record.error_code = "interrupted"
                record.next_retry_at = now
                record.updated_at = now
                record.save()
        return len(running_records)

    @staticmethod
    def _finalize_attempt(
        attempt: ResourceTaskAttempt,
        state: str,
        *,
        finished_at,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        attempt.state = state
        attempt.finished_at = finished_at
        attempt.error_code = error_code
        attempt.error_message = error_message
        attempt.retryable = retryable
        attempt.save()


class ResourceTaskRunner:
    @classmethod
    def _run_single(
        cls, spec: ResourceTaskSpec, ctx: RunContext, resource: Any
    ) -> tuple[str, TaskAbortError | None]:
        """单资源完整生命周期，返回 (统计桶, abort 异常或 None)。线程安全（无共享写）。"""
        resource_id = spec.resource_id_of(resource)
        attempt, record, prior_state = ResourceTaskLedger.begin_attempt(
            task_key=spec.task_key,
            resource_type=spec.resource_type,
            resource_id=resource_id,
            trigger_type=ctx.trigger_type,
            task_run_id=ctx.task_run_id,
        )
        try:
            spec.process_one(ctx, resource)
        except TaskItemDeferred as exc:
            ResourceTaskLedger.finish_non_consuming(
                attempt,
                record,
                prior_state=prior_state,
                attempt_state="deferred",
                error_code=exc.error_code,
                error_message=str(exc),
            )
            return "deferred_count", None
        except TaskAbortError as exc:
            # 任务级中止：当前资源不耗预算，run 交由上层 run_task 记失败。
            ResourceTaskLedger.finish_non_consuming(
                attempt,
                record,
                prior_state=prior_state,
                attempt_state="aborted",
                error_code=exc.error_code,
                error_message=str(exc),
            )
            logger.warning(
                "Resource task aborted task_key={} resource_id={} code={}",
                spec.task_key,
                resource_id,
                exc.error_code,
            )
            return "aborted", exc
        except Exception as exc:
            if isinstance(exc, TaskItemError):
                error_code, retryable = exc.error_code, exc.retryable
            else:
                error_code, retryable = ERROR_CODE_UNHANDLED, True
            budget_exempt = bool(spec.retry.exempt and spec.retry.exempt(resource))
            final_state = ResourceTaskLedger.finish_failure(
                attempt,
                record,
                error_code=error_code,
                error_message=str(exc),
                retryable=retryable,
                policy=spec.retry,
                budget_exempt=budget_exempt,
            )
            return f"{final_state}_count", None
        ResourceTaskLedger.finish_success(attempt, record)
        return "succeeded_count", None

    @classmethod
    def run(
        cls,
        spec: ResourceTaskSpec,
        reporter,
        *,
        only_ids: list[int] | None = None,
    ) -> dict[str, int]:
        import threading

        from src.service.system.activity_service import ActivityService

        context = ActivityService.get_task_run_context()
        ctx = RunContext(
            task_key=spec.task_key,
            reporter=reporter,
            trigger_type=getattr(context, "trigger_type", None),
            task_run_id=getattr(context, "task_run_id", None),
        )
        if spec.setup_run is not None:
            ctx.shared = spec.setup_run(ctx)

        state_condition = ResourceTaskLedger.eligible_state_condition
        candidates = list(spec.select_candidates(state_condition, only_ids=only_ids))
        stats = {
            "candidate_count": len(candidates),
            "succeeded_count": 0,
            "failed_retryable_count": 0,
            "failed_terminal_count": 0,
            "exhausted_count": 0,
            "deferred_count": 0,
        }
        total = len(candidates)
        progress_lock = threading.Lock()
        completed_count = 0

        def record_outcome(bucket: str) -> None:
            nonlocal completed_count
            with progress_lock:
                if bucket != "aborted":
                    stats[bucket] += 1
                completed_count += 1
                current = completed_count
            reporter.emit(current=current, total=total)

        concurrency = max(int(spec.concurrency or 1), 1)
        if concurrency == 1 or len(candidates) <= 1:
            for resource in candidates:
                bucket, abort_error = cls._run_single(spec, ctx, resource)
                record_outcome(bucket)
                if abort_error is not None:
                    raise abort_error
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            from src.common.database import ensure_database_ready

            def worker(resource: Any) -> tuple[str, TaskAbortError | None]:
                # 每 worker 线程自建 DB 连接；ctx 显式传参，不依赖 ContextVar 继承。
                ensure_database_ready()
                return cls._run_single(spec, ctx, resource)

            first_abort: TaskAbortError | None = None
            with ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix=f"resource-task-{spec.task_key}",
            ) as pool:
                futures = [pool.submit(worker, resource) for resource in candidates]
                for future in as_completed(futures):
                    bucket, abort_error = future.result()
                    record_outcome(bucket)
                    if abort_error is not None and first_abort is None:
                        first_abort = abort_error
            if first_abort is not None:
                # 并发模式下无法拦截已提交项：收齐全部结果后再抛出中止。
                raise first_abort

        if isinstance(ctx.shared, dict):
            # setup_run 产出的共享上下文若是 dict，回传给任务侧合并领域计数。
            stats["shared"] = ctx.shared
        return stats
