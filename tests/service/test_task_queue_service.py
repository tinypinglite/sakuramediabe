"""任务队列内核（Wave 1）语义护栏：入队 coalesce / 领取范围 / 租约回收。

这些语义是新执行内核的地基，坏了会以"任务不跑/重复跑"的形式静默出现，
所以按护栏测试维护。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun
from src.service.system.task_queue_service import (
    FAILURE_CODE_QUEUE_LEASE_EXPIRED,
    INTERNAL_FAILURE_CODE_KEY,
    TaskQueueConflictError,
    TaskQueueService,
)


def _create_expired_running_run(*, task_key: str) -> BackgroundTaskRun:
    queued = TaskQueueService.enqueue(task_key=task_key, trigger_type="scheduled")
    claimed = TaskQueueService.claim_next(lease_seconds=60)
    assert claimed is not None and claimed.id == queued.id
    BackgroundTaskRun.update(
        lease_expires_at=utc_now_for_db() - timedelta(seconds=1)
    ).where(BackgroundTaskRun.id == queued.id).execute()
    return queued


def test_enqueue_creates_pending_queue_row(test_db):
    task_run = TaskQueueService.enqueue(
        task_key="movie_heat_update",
        trigger_type="scheduled",
        params={"force": True},
    )

    assert task_run is not None
    assert task_run.state == "pending"
    assert task_run.mutex_key == "aps:movie_heat_update"
    assert task_run.scheduled_at is not None
    stored = BackgroundTaskRun.get_by_id(task_run.id)
    assert stored.params == {"force": True}


def test_enqueue_scheduled_coalesces_when_same_task_is_queued(test_db):
    first = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    second = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")

    assert first is not None
    assert second is None
    assert BackgroundTaskRun.select().count() == 1


def test_enqueue_manual_conflict_raises_with_blocking_run_id(test_db):
    blocking = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")

    with pytest.raises(TaskQueueConflictError) as exc_info:
        TaskQueueService.enqueue(
            task_key="movie_heat_update", trigger_type="manual", conflict="raise"
        )

    assert exc_info.value.blocking_task_run_id == blocking.id


def test_enqueue_allowed_again_after_terminal_state_releases_mutex(test_db):
    from src.service.system.activity_service import ActivityService

    first = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    ActivityService.fail_task_run(first.id, error_message="boom", notify_result=False)

    second = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")

    assert second is not None
    assert second.id != first.id


def test_claim_next_claims_earliest_due_row_and_sets_lease(test_db):
    first = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    # 用不同 task_key 绕过 scheduled coalesce，验证按 id 顺序领取两行。
    second = TaskQueueService.enqueue(task_key="hot_review_sync", trigger_type="scheduled")

    claimed = TaskQueueService.claim_next(lease_seconds=120)

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.state == "running"
    assert claimed.started_at is not None
    assert claimed.lease_expires_at is not None
    # 第二次领取拿到下一行，队列排空后返回 None。
    assert TaskQueueService.claim_next().id == second.id
    assert TaskQueueService.claim_next() is None


def test_claim_next_ignores_non_queue_rows_and_future_schedules(test_db):
    from src.service.system.activity_service import ActivityService

    # 进程内直跑的 task_run（scheduled_at 为空）不属于队列，worker 不得领取。
    ActivityService.create_task_run(
        task_key="media_directory_import", trigger_type="manual", state="pending"
    )
    TaskQueueService.enqueue(
        task_key="movie_heat_update",
        trigger_type="scheduled",
        scheduled_at=utc_now_for_db() + timedelta(hours=1),
    )

    assert TaskQueueService.claim_next() is None


def test_recover_expired_leases_fails_run_and_releases_mutex(test_db):
    stale = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    TaskQueueService.claim_next(lease_seconds=60)
    # 直接把租约拨到过去，模拟持有者进程死亡后停止续租。
    BackgroundTaskRun.update(
        lease_expires_at=utc_now_for_db() - timedelta(seconds=1)
    ).where(BackgroundTaskRun.id == stale.id).execute()
    # running 行仍持有 mutex，必须换 task_key 才能再入队一行为"健康对照"。
    healthy = TaskQueueService.enqueue(task_key="hot_review_sync", trigger_type="scheduled")
    TaskQueueService.claim_next(lease_seconds=3600)

    recovered = TaskQueueService.recover_expired_leases()

    assert [run.id for run in recovered] == [stale.id]
    stale_row = BackgroundTaskRun.get_by_id(stale.id)
    assert stale_row.state == "failed"
    assert stale_row.mutex_key is None
    assert stale_row.lease_expires_at is None
    assert (
        stale_row.result_summary[INTERNAL_FAILURE_CODE_KEY]
        == FAILURE_CODE_QUEUE_LEASE_EXPIRED
    )
    # 租约未过期的 running 行不受影响。
    healthy_row = BackgroundTaskRun.get_by_id(healthy.id)
    assert healthy_row.state == "running"
    # mutex 释放后同 task_key 可再次入队。
    assert TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled") is not None


def test_renew_leases_extends_running_rows_only(test_db):
    run = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    TaskQueueService.claim_next(lease_seconds=60)
    before = BackgroundTaskRun.get_by_id(run.id).lease_expires_at

    renewed_count = TaskQueueService.renew_leases([run.id], lease_seconds=3600)

    after = BackgroundTaskRun.get_by_id(run.id).lease_expires_at
    assert renewed_count == 1
    assert after > before
    # pending 行不续租。
    # running 行仍持有 mutex，用另一个 task_key 造 pending 对照行。
    queued = TaskQueueService.enqueue(task_key="hot_review_sync", trigger_type="scheduled")
    assert TaskQueueService.renew_leases([queued.id]) == 0


def test_bootstrap_blocker_settlement_is_restricted_to_builtin_bootstrap_keys(test_db):
    queued = TaskQueueService.enqueue(
        task_key="movie_heat_update",
        trigger_type="scheduled",
    )

    with pytest.raises(ValueError, match="unsupported_bootstrap_task_key"):
        TaskQueueService.settle_bootstrap_blocker(
            task_key="movie_heat_update",
            task_run_id=queued.id,
        )


@pytest.mark.parametrize("terminal_state", ["completed", "failed"])
def test_normal_terminal_transition_clears_queue_lease(test_db, terminal_state):
    from src.service.system import ActivityService

    queued = TaskQueueService.enqueue(
        task_key=f"terminal-clears-lease-{terminal_state}",
        trigger_type="scheduled",
    )
    TaskQueueService.claim_next(lease_seconds=3600)

    if terminal_state == "completed":
        ActivityService.complete_task_run(queued.id, notify_result=False)
    else:
        ActivityService.fail_task_run(
            queued.id,
            error_message="terminal",
            notify_result=False,
        )

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert stored.state == terminal_state
    assert stored.mutex_key is None
    assert stored.lease_expires_at is None


def test_expired_lease_recovery_skips_row_when_heartbeat_holds_lock(test_db):
    from src.model import SystemNotification

    queued = _create_expired_running_run(task_key="lease-heartbeat-first")
    heartbeat_has_lock = Event()
    release_heartbeat = Event()

    def heartbeat() -> int:
        with test_db.connection_context(), test_db.atomic():
            (
                BackgroundTaskRun.select()
                .where(BackgroundTaskRun.id == queued.id)
                .for_update()
                .get()
            )
            updated = (
                BackgroundTaskRun.update(
                    lease_expires_at=utc_now_for_db() + timedelta(hours=1)
                )
                .where(
                    BackgroundTaskRun.id == queued.id,
                    BackgroundTaskRun.state == "running",
                )
                .execute()
            )
            heartbeat_has_lock.set()
            assert release_heartbeat.wait(5)
            return updated

    with ThreadPoolExecutor(max_workers=1) as pool:
        heartbeat_future = pool.submit(heartbeat)
        assert heartbeat_has_lock.wait(5)
        recovered = TaskQueueService.recover_expired_leases()
        release_heartbeat.set()
        assert heartbeat_future.result() == 1

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert recovered == []
    assert stored.state == "running"
    assert stored.lease_expires_at > utc_now_for_db()
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == queued.id
    ).count() == 0


def test_expired_lease_recovery_wins_before_heartbeat_update(
    test_db, monkeypatch
):
    from src.model import SystemNotification
    from src.service.system.activity.task_runs import TaskRunService

    queued = _create_expired_running_run(task_key="lease-recovery-first")
    recovery_has_lock = Event()
    release_recovery = Event()
    heartbeat_started = Event()
    original_fail_task_run = TaskRunService.fail_task_run

    def delayed_fail_task_run(cls, task_run_id, **kwargs):
        recovery_has_lock.set()
        assert release_recovery.wait(5)
        return original_fail_task_run(task_run_id, **kwargs)

    monkeypatch.setattr(
        TaskRunService,
        "fail_task_run",
        classmethod(delayed_fail_task_run),
    )

    def recover():
        with test_db.connection_context():
            return TaskQueueService.recover_expired_leases()

    def heartbeat() -> int:
        with test_db.connection_context():
            heartbeat_started.set()
            return TaskQueueService.renew_leases([queued.id], lease_seconds=3600)

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery_future = pool.submit(recover)
        assert recovery_has_lock.wait(5)
        heartbeat_future = pool.submit(heartbeat)
        assert heartbeat_started.wait(5)
        release_recovery.set()
        recovered = recovery_future.result()
        heartbeat_updated = heartbeat_future.result()

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert [run.id for run in recovered] == [queued.id]
    assert heartbeat_updated == 0
    assert stored.state == "failed"
    assert stored.mutex_key is None
    assert stored.lease_expires_at is None
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == queued.id
    ).count() == 1


def test_concurrent_expired_lease_recovery_has_single_winner_and_notification(test_db):
    from src.model import SystemNotification

    queued = _create_expired_running_run(task_key="lease-recovery-race")
    barrier = Barrier(2)

    def recover() -> list[int]:
        with test_db.connection_context():
            barrier.wait()
            return [
                run.id for run in TaskQueueService.recover_expired_leases()
            ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recover)
        second = pool.submit(recover)
        results = [first.result(), second.result()]

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert sorted(results, key=len) == [[], [queued.id]]
    assert stored.state == "failed"
    assert stored.lease_expires_at is None
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == queued.id
    ).count() == 1
