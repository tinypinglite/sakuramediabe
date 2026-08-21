from src.model import BackgroundTaskRun
from src.service.system import ActivityCleanupService, ActivityService


def test_activity_cleanup_deletes_only_terminal_task_runs(test_db):
    pending = ActivityService.create_task_run(
        task_key="cleanup-active-guard",
        trigger_type="manual",
        state="pending",
    )
    running = ActivityService.create_task_run(
        task_key="cleanup-active-guard",
        trigger_type="manual",
        state="running",
    )
    terminal_ids = []
    for index in range(3):
        task_run = ActivityService.create_task_run(
            task_key="cleanup-active-guard",
            trigger_type="manual",
            state="pending",
        )
        if index % 2:
            ActivityService.fail_task_run(
                task_run.id,
                error_message="terminal",
                notify_result=False,
            )
        else:
            ActivityService.complete_task_run(task_run.id, notify_result=False)
        terminal_ids.append(task_run.id)

    deleted_count = ActivityCleanupService()._cleanup_task_runs(1)

    assert deleted_count == 2
    assert BackgroundTaskRun.get_by_id(pending.id).state == "pending"
    assert BackgroundTaskRun.get_by_id(running.id).state == "running"
    assert (
        not BackgroundTaskRun.select()
        .where(BackgroundTaskRun.id.in_(terminal_ids[:2]))
        .exists()
    )
    assert BackgroundTaskRun.get_by_id(terminal_ids[-1]).state == "completed"
