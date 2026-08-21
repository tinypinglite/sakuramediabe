"""基于 BackgroundTaskRun 的持久任务队列内核。

pending 的 BackgroundTaskRun 行即队列元素，四个原语：

- ``enqueue``：建 pending 行并占 mutex。mutex_key 唯一索引保证同 task_key 最多一个
  pending/running 行，scheduled 触发的 coalesce（积压即丢弃）由此天然成立；
- ``claim_next``：``FOR UPDATE SKIP LOCKED`` 领取最早到期的 pending 行，置 running
  并发放租约；
- ``renew_leases``：执行中由 worker 周期续租；
- ``recover_expired_leases``：租约过期即回收（取代 owner_pid 判活）。

``scheduled_at`` 非空是"队列托管行"的判别标志：导入族等仍在进程内直跑的 task_run
（scheduled_at 为空）不进入领取范围，也不参与租约回收之外的队列语义。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from peewee import IntegrityError, fn

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun
from src.model.base import get_database
from src.service.system.activity.task_runs import TaskRunService

# 与 APS 现有互斥命名空间保持一致：队列行与遗留直跑行竞争同一把锁，
# 过渡期两种执行方式也不会同 task_key 并发。
QUEUE_MUTEX_PREFIX = "aps:"
DEFAULT_LEASE_SECONDS = 300
LEASE_EXPIRED_ERROR_MESSAGE = "任务租约过期，执行进程已中断，任务按失败回收"
BOOTSTRAP_LEASE_EXPIRED_ERROR_MESSAGE = "启动引导任务租约过期，已回收并重新入队"
INTERNAL_FAILURE_CODE_KEY = "_failure_code"
FAILURE_CODE_QUEUE_LEASE_EXPIRED = "queue_lease_expired"
BOOTSTRAP_QUEUE_TASK_KEYS = frozenset(
    {"gfriends_filetree_refresh", "movie_similarity_recompute"}
)


class TaskQueueConflictError(RuntimeError):
    """入队冲突：同 task_key 已有 pending/running 行持有 mutex。"""

    def __init__(self, task_key: str, blocking_task_run_id: int | None):
        super().__init__(f"task_queue_conflict task_key={task_key}")
        self.task_key = task_key
        self.blocking_task_run_id = blocking_task_run_id


class TaskQueueService:
    @staticmethod
    def build_mutex_key(task_key: str) -> str:
        return f"{QUEUE_MUTEX_PREFIX}{task_key}"

    @classmethod
    def enqueue(
        cls,
        *,
        task_key: str,
        trigger_type: str,
        task_name: str | None = None,
        params: dict[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        conflict: str = "skip",
    ) -> BackgroundTaskRun | None:
        """入队一次执行。conflict="skip" 返回 None（scheduled 的 coalesce 语义），
        conflict="raise" 抛 TaskQueueConflictError 并带上阻塞方 run id（manual 用）。"""
        if conflict not in ("skip", "raise"):
            raise ValueError(f"unsupported_conflict_policy: {conflict}")
        mutex_key = cls.build_mutex_key(task_key)
        try:
            return TaskRunService.create_task_run(
                task_key=task_key,
                task_name=task_name,
                trigger_type=trigger_type,
                state="pending",
                mutex_key=mutex_key,
                params=params,
                scheduled_at=scheduled_at or utc_now_for_db(),
            )
        except IntegrityError:
            if conflict == "skip":
                return None
            blocking = TaskRunService.find_task_run_by_mutex_key(mutex_key)
            raise TaskQueueConflictError(
                task_key, blocking.id if blocking is not None else None
            ) from None

    @classmethod
    def publish_run(
        cls,
        task_run_id: int,
        *,
        params: dict[str, Any] | None = None,
    ) -> None:
        """两阶段发布的第二步：补齐 params 并置 scheduled_at，行自此可被 worker 领取。

        触发方需要先建行占 mutex、再准备完整执行参数时，
        用 scheduled_at 为空的 pending 行做"未发布"状态，避免 worker 抢跑读到半成品。
        """
        BackgroundTaskRun.update(
            params=params,
            scheduled_at=utc_now_for_db(),
            updated_at=utc_now_for_db(),
        ).where(BackgroundTaskRun.id == task_run_id).execute()

    @classmethod
    def settle_bootstrap_blocker(
        cls,
        *,
        task_key: str,
        task_run_id: int,
    ) -> BackgroundTaskRun | None:
        """锁定并收敛 bootstrap 冲突行，只回收两个内建引导任务的过期租约。

        返回锁内最新快照；行不存在或已不属于目标 task_key 时返回 None。过期判断与
        失败终态写入位于同一 PostgreSQL 行锁事务，避免先扫 lease、后强制回收时误伤
        已续租任务的 TOCTOU 窗口。
        """
        if task_key not in BOOTSTRAP_QUEUE_TASK_KEYS:
            raise ValueError(f"unsupported_bootstrap_task_key: {task_key}")

        current = utc_now_for_db()
        with get_database().atomic():
            task_run = (
                BackgroundTaskRun.select()
                .where(
                    BackgroundTaskRun.id == task_run_id,
                    BackgroundTaskRun.task_key == task_key,
                )
                .for_update()
                .first()
            )
            if task_run is None:
                return None
            if (
                task_run.state == "running"
                and task_run.scheduled_at is not None
                and task_run.lease_expires_at is not None
                and task_run.lease_expires_at < current
            ):
                # 只处理当前 bootstrap 冲突行；其它过期租约仍由 worker housekeeper 负责。
                TaskRunService.fail_task_run(
                    task_run.id,
                    error_message=BOOTSTRAP_LEASE_EXPIRED_ERROR_MESSAGE,
                    result_summary={
                        INTERNAL_FAILURE_CODE_KEY: FAILURE_CODE_QUEUE_LEASE_EXPIRED
                    },
                )
                task_run = BackgroundTaskRun.get_by_id(task_run.id)
            return task_run

    @classmethod
    def claim_next(
        cls,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        include_task_keys: set[str] | None = None,
        exclude_task_keys: set[str] | None = None,
    ) -> BackgroundTaskRun | None:
        """领取最早到期的队列行并置 running + 租约；无可领取行返回 None。

        SKIP LOCKED 保证多 worker 并发领取互不阻塞、也不会重复领取同一行。
        include/exclude 供并发道（lane）按 task_key 划分领取范围。
        """
        current = utc_now_for_db()
        conditions = [
            BackgroundTaskRun.state == "pending",
            BackgroundTaskRun.scheduled_at.is_null(False),
            BackgroundTaskRun.scheduled_at <= current,
        ]
        if include_task_keys:
            conditions.append(BackgroundTaskRun.task_key.in_(tuple(include_task_keys)))
        if exclude_task_keys:
            conditions.append(
                BackgroundTaskRun.task_key.not_in(tuple(exclude_task_keys))
            )
        candidate = (
            BackgroundTaskRun.select(BackgroundTaskRun.id)
            .where(*conditions)
            .order_by(BackgroundTaskRun.id.asc())
            .limit(1)
            .for_update("FOR UPDATE SKIP LOCKED")
        )
        with get_database().atomic():
            claimed = list(
                BackgroundTaskRun.update(
                    state="running",
                    started_at=fn.COALESCE(BackgroundTaskRun.started_at, current),
                    lease_expires_at=current + timedelta(seconds=lease_seconds),
                    updated_at=current,
                )
                .where(BackgroundTaskRun.id.in_(candidate))
                .returning(BackgroundTaskRun)
                .execute()
            )
        if not claimed:
            return None
        return claimed[0]

    @classmethod
    def renew_leases(
        cls,
        task_run_ids: Iterable[int],
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> int:
        run_ids = list(task_run_ids)
        if not run_ids:
            return 0
        current = utc_now_for_db()
        return (
            BackgroundTaskRun.update(
                lease_expires_at=current + timedelta(seconds=lease_seconds),
                updated_at=current,
            )
            .where(
                BackgroundTaskRun.id.in_(run_ids),
                BackgroundTaskRun.state == "running",
            )
            .execute()
        )

    @classmethod
    def recover_expired_leases(
        cls, *, error_message: str = LEASE_EXPIRED_ERROR_MESSAGE
    ) -> list[BackgroundTaskRun]:
        """在单个 PostgreSQL 事务内锁定并回收租约过期的 running 行。

        固定一次 ``now``，候选通过 ``FOR UPDATE SKIP LOCKED`` 领取并在锁内再次校验；
        heartbeat 先持锁/续租则本轮跳过，recovery 先持锁则 heartbeat 的 state 条件更新
        为 0，并发 recovery 也只有持锁方能完成终态与通知。领域侧联动仍由调用方处理。
        """
        current = utc_now_for_db()
        recovered: list[BackgroundTaskRun] = []
        with get_database().atomic():
            candidates = (
                BackgroundTaskRun.select()
                .where(
                    BackgroundTaskRun.scheduled_at.is_null(False),
                    BackgroundTaskRun.state == "running",
                    BackgroundTaskRun.lease_expires_at.is_null(False),
                    BackgroundTaskRun.lease_expires_at < current,
                )
                .order_by(BackgroundTaskRun.id.asc())
                .for_update("FOR UPDATE SKIP LOCKED")
            )
            for task_run in candidates:
                # PostgreSQL 在锁等待后会重检 WHERE；这里再按同一个 fixed now 明确护栏。
                if (
                    task_run.scheduled_at is None
                    or task_run.state != "running"
                    or task_run.lease_expires_at is None
                    or task_run.lease_expires_at >= current
                ):
                    continue
                recovered_run = TaskRunService.fail_task_run(
                    task_run.id,
                    error_message=error_message,
                    result_summary={
                        INTERNAL_FAILURE_CODE_KEY: FAILURE_CODE_QUEUE_LEASE_EXPIRED
                    },
                )
                if recovered_run.state == "failed":
                    recovered.append(recovered_run)
        return recovered
