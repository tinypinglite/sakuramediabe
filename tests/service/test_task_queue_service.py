"""任务队列内核（Wave 1）语义护栏：入队 coalesce / 领取范围 / 租约回收。

这些语义是新执行内核的地基，坏了会以"任务不跑/重复跑"的形式静默出现，
所以按护栏测试维护。
"""

from datetime import timedelta

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun
from src.service.system.task_queue_service import (
    TaskQueueConflictError,
    TaskQueueService,
)


def test_enqueue_creates_pending_queue_row(test_db):
    task_run = TaskQueueService.enqueue(
        task_key="movie_heat_update",
        trigger_type="scheduled",
        params={"only_ids": [1, 2]},
    )

    assert task_run is not None
    assert task_run.state == "pending"
    assert task_run.mutex_key == "aps:movie_heat_update"
    assert task_run.scheduled_at is not None
    stored = BackgroundTaskRun.get_by_id(task_run.id)
    assert stored.params == {"only_ids": [1, 2]}


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
