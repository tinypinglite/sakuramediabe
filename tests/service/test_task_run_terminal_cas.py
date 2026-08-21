"""TaskRun 状态转移的 PostgreSQL 行锁与终态幂等护栏。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.model import BackgroundTaskRun, SystemNotification
from src.service.system import ActivityService
from src.service.system.activity import NotificationDraft, NotificationService
from src.service.system.activity.task_execution import TaskRunFinalizedError


def _create_pending_run(*, task_key: str, mutex_key: str) -> BackgroundTaskRun:
    return ActivityService.create_task_run(
        task_key=task_key,
        task_name=task_key,
        trigger_type="manual",
        mutex_key=mutex_key,
    )


@pytest.mark.parametrize("terminal_state", ["completed", "failed"])
def test_mark_running_never_revives_terminal_run(test_db, terminal_state):
    run = _create_pending_run(
        task_key=f"mark-terminal-{terminal_state}",
        mutex_key=f"mark-terminal:{terminal_state}",
    )
    if terminal_state == "completed":
        ActivityService.complete_task_run(run.id, notify_result=False)
    else:
        ActivityService.fail_task_run(
            run.id, error_message="terminal", notify_result=False
        )
    before = BackgroundTaskRun.get_by_id(run.id)

    returned = ActivityService.mark_task_run_running(run.id)
    after = BackgroundTaskRun.get_by_id(run.id)

    assert returned.state == terminal_state
    assert after.state == terminal_state
    assert after.started_at == before.started_at
    assert after.finished_at == before.finished_at
    assert after.updated_at == before.updated_at


def test_repeated_mark_running_does_not_rewrite_running_row(test_db):
    run = _create_pending_run(task_key="mark-running-once", mutex_key="mark-running-once")
    first = ActivityService.mark_task_run_running(run.id)
    before = BackgroundTaskRun.get_by_id(run.id)

    second = ActivityService.mark_task_run_running(run.id)
    after = BackgroundTaskRun.get_by_id(run.id)

    assert first.state == "running"
    assert second.state == "running"
    assert after.started_at == before.started_at
    assert after.updated_at == before.updated_at


def test_completed_run_rejects_duplicate_complete_and_late_fail(test_db):
    mutex_key = "terminal-cas:completed"
    run = _create_pending_run(task_key="terminal-completed", mutex_key=mutex_key)
    ActivityService.complete_task_run(
        run.id,
        result_summary={"failed_count": 1, "winner": "completed"},
        result_text="completed winner",
    )
    winner = BackgroundTaskRun.get_by_id(run.id)

    duplicate = ActivityService.complete_task_run(
        run.id,
        result_summary={"duplicate_complete": True},
        result_text="duplicate",
    )
    late_failure = ActivityService.fail_task_run(
        run.id,
        error_message="late failure",
        result_summary={"late_fail": True},
    )
    stored = BackgroundTaskRun.get_by_id(run.id)

    assert duplicate.state == "completed"
    assert late_failure.state == "completed"
    assert stored.state == "completed"
    assert stored.result_summary == {"failed_count": 1, "winner": "completed"}
    assert stored.result_text == "completed winner"
    assert stored.error_message is None
    assert stored.finished_at == winner.finished_at
    assert stored.updated_at == winner.updated_at
    assert stored.mutex_key is None
    notification = SystemNotification.get(
        SystemNotification.related_task_run == run.id
    )
    assert notification.category == "warning"
    assert notification.dedupe_key == f"task_run_result:{run.id}"
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == run.id
    ).count() == 1
    # 终态提交已原子释放 mutex，同一互斥键可以立即复用。
    assert _create_pending_run(
        task_key="terminal-completed-next", mutex_key=mutex_key
    ).mutex_key == mutex_key


def test_failed_run_rejects_duplicate_fail_and_late_complete(test_db):
    mutex_key = "terminal-cas:failed"
    run = _create_pending_run(task_key="terminal-failed", mutex_key=mutex_key)
    ActivityService.fail_task_run(
        run.id,
        error_message="first failure",
        result_summary={"winner": "failed"},
    )
    winner = BackgroundTaskRun.get_by_id(run.id)

    duplicate = ActivityService.fail_task_run(
        run.id,
        error_message="duplicate failure",
        result_summary={"duplicate_fail": True},
    )
    late_complete = ActivityService.complete_task_run(
        run.id,
        result_summary={"late_complete": True, "failed_count": 1},
    )
    stored = BackgroundTaskRun.get_by_id(run.id)

    assert duplicate.state == "failed"
    assert late_complete.state == "failed"
    assert stored.state == "failed"
    assert stored.error_message == "first failure"
    assert stored.result_summary == {"winner": "failed"}
    assert stored.result_text is None
    assert stored.finished_at == winner.finished_at
    assert stored.updated_at == winner.updated_at
    assert stored.mutex_key is None
    notification = SystemNotification.get(
        SystemNotification.related_task_run == run.id
    )
    assert notification.category == "error"
    assert notification.content == "first failure"
    assert notification.dedupe_key == f"task_run_result:{run.id}"
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == run.id
    ).count() == 1
    assert _create_pending_run(
        task_key="terminal-failed-next", mutex_key=mutex_key
    ).mutex_key == mutex_key


def test_complete_and_fail_race_has_single_terminal_winner_and_notification(test_db):
    run = _create_pending_run(
        task_key="terminal-race",
        mutex_key="terminal-cas:race",
    )
    barrier = Barrier(2)

    def complete() -> str:
        # 每条线程使用 Peewee 的线程本地连接，真实触发 PostgreSQL 行锁竞争。
        with test_db.connection_context():
            barrier.wait()
            return ActivityService.complete_task_run(
                run.id,
                result_summary={
                    "winner": "completed",
                    "failed_count": 1,
                },
            ).state

    def fail() -> str:
        with test_db.connection_context():
            barrier.wait()
            return ActivityService.fail_task_run(
                run.id,
                error_message="concurrent failure",
                result_summary={"winner": "failed"},
            ).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        complete_future = pool.submit(complete)
        fail_future = pool.submit(fail)
        returned_states = {complete_future.result(), fail_future.result()}

    stored = BackgroundTaskRun.get_by_id(run.id)
    assert returned_states == {stored.state}
    assert stored.state in {"completed", "failed"}
    assert stored.result_summary["winner"] == stored.state
    assert stored.mutex_key is None
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == run.id
    ).count() == 1


def test_terminal_run_rejects_progress_without_any_write(test_db):
    run = _create_pending_run(
        task_key="terminal-progress",
        mutex_key="terminal-progress",
    )
    ActivityService.fail_task_run(
        run.id,
        error_message="terminal",
        result_summary={"winner": "failed"},
        notify_result=False,
    )
    before = BackgroundTaskRun.get_by_id(run.id)

    returned = ActivityService.update_task_run_progress(
        run.id,
        current=99,
        total=100,
        text="late progress",
        summary_patch={"late": True},
    )
    after = BackgroundTaskRun.get_by_id(run.id)

    assert returned.state == "failed"
    assert after.state == "failed"
    assert after.progress_current == before.progress_current
    assert after.progress_total == before.progress_total
    assert after.progress_text == before.progress_text
    assert after.result_summary == before.result_summary
    assert after.error_message == before.error_message
    assert after.finished_at == before.finished_at
    assert after.updated_at == before.updated_at
    assert after.mutex_key == before.mutex_key


def test_progress_and_terminal_race_never_revives_stale_active_snapshot(test_db):
    run = _create_pending_run(
        task_key="progress-terminal-race",
        mutex_key="progress-terminal-race",
    )
    stale = BackgroundTaskRun.get_by_id(run.id)
    barrier = Barrier(2)

    def progress() -> str:
        with test_db.connection_context():
            barrier.wait()
            return ActivityService.update_task_run_progress(
                stale.id,
                current=1,
                total=2,
                text="racing",
                summary_patch={"progress_seen": True},
            ).state

    def complete() -> str:
        with test_db.connection_context():
            barrier.wait()
            return ActivityService.complete_task_run(
                run.id,
                result_summary={"winner": "completed"},
                notify_result=False,
            ).state

    with ThreadPoolExecutor(max_workers=2) as pool:
        progress_future = pool.submit(progress)
        complete_future = pool.submit(complete)
        progress_future.result()
        complete_future.result()

    stored = BackgroundTaskRun.get_by_id(run.id)
    assert stale.state == "pending"
    assert stored.state == "completed"
    assert stored.result_summary["winner"] == "completed"
    assert stored.finished_at is not None
    assert stored.mutex_key is None


@pytest.mark.parametrize("terminal_state", ["completed", "failed"])
def test_run_task_does_not_execute_terminal_run(test_db, terminal_state):
    run = _create_pending_run(
        task_key=f"run-terminal-{terminal_state}",
        mutex_key=f"run-terminal-{terminal_state}",
    )
    if terminal_state == "completed":
        ActivityService.complete_task_run(run.id, notify_result=False)
    else:
        ActivityService.fail_task_run(
            run.id, error_message="already failed", notify_result=False
        )
    calls: list[str] = []

    with pytest.raises(TaskRunFinalizedError) as exc_info:
        ActivityService.run_task(
            task_run_id=run.id,
            func=lambda _reporter: calls.append("executed"),
            notify_result=False,
        )

    assert calls == []
    assert exc_info.value.task_run.state == terminal_state
    assert BackgroundTaskRun.get_by_id(run.id).state == terminal_state


def test_run_task_success_observes_persisted_failed_winner(test_db):
    run = _create_pending_run(
        task_key="run-success-loses-to-fail",
        mutex_key="run-success-loses-to-fail",
    )

    def func(_reporter):
        ActivityService.fail_task_run(
            run.id,
            error_message="persisted failure",
            result_summary={"winner": "failed"},
            notify_result=False,
        )
        return {"local": "success"}

    with pytest.raises(TaskRunFinalizedError) as exc_info:
        ActivityService.run_task(
            task_run_id=run.id,
            func=func,
            notify_result=False,
        )

    assert exc_info.value.task_run.state == "failed"
    assert str(exc_info.value.task_run.error_message) == "persisted failure"
    stored = BackgroundTaskRun.get_by_id(run.id)
    assert stored.state == "failed"
    assert stored.result_summary == {"winner": "failed"}


def test_run_task_exception_observes_persisted_completed_winner(test_db):
    run = _create_pending_run(
        task_key="run-failure-loses-to-complete",
        mutex_key="run-failure-loses-to-complete",
    )

    def func(_reporter):
        ActivityService.complete_task_run(
            run.id,
            result_summary={"winner": "completed"},
            notify_result=False,
        )
        raise RuntimeError("late local failure")

    result = ActivityService.run_task(
        task_run_id=run.id,
        func=func,
        notify_result=False,
    )

    assert result == {"winner": "completed"}
    stored = BackgroundTaskRun.get_by_id(run.id)
    assert stored.state == "completed"
    assert stored.result_summary == {"winner": "completed"}
    assert stored.error_message is None


def test_notification_create_once_has_single_winner_across_connections(test_db):
    draft = NotificationDraft(
        category="warning",
        title="并发通知",
        content="只应创建一次",
        dedupe_key="notification:create-once:concurrent",
    )
    barrier = Barrier(2)

    def create_once() -> int:
        with test_db.connection_context():
            barrier.wait()
            return NotificationService.create_once(draft).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create_once)
        second = pool.submit(create_once)
        notification_ids = {first.result(), second.result()}

    assert len(notification_ids) == 1
    assert SystemNotification.select().where(
        SystemNotification.dedupe_key == draft.dedupe_key
    ).count() == 1
