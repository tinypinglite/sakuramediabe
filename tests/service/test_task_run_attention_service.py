"""任务运行 scope、待关注聚合、告警识别与高频成功历史压缩测试。"""

from datetime import timedelta

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, SystemNotification
from src.service.system import ActivityService
from src.service.system.activity.task_summary import has_failed_summary


def _create_run(
    *,
    task_key: str,
    state: str,
    trigger_type: str = "manual",
    created_at=None,
    result_summary: dict | None = None,
) -> BackgroundTaskRun:
    created = created_at or utc_now_for_db()
    return BackgroundTaskRun.create(
        task_key=task_key,
        task_name=task_key,
        trigger_type=trigger_type,
        state=state,
        created_at=created,
        started_at=created,
        finished_at=created,
        result_summary=result_summary or {},
    )


def test_to_task_run_resource_has_warning_matches_notification_semantics(test_db):
    cases = [
        ({"failed_count": 1}, True),
        ({"failed_items": 2, "succeeded_count": 5}, True),
        ({"failed_count": 0}, False),
        ({"failed_items": []}, False),
        ({"succeeded_count": 5}, False),
        ({}, False),
        (None, False),
    ]
    for index, (summary, expected) in enumerate(cases):
        run = BackgroundTaskRun.create(
            task_key=f"warning_case_{index}",
            task_name="warning",
            trigger_type="manual",
            state="completed",
            result_summary=summary or {},
        )
        resource = ActivityService.to_task_run_resource(run)

        assert resource.has_warning is expected
        assert resource.has_warning is has_failed_summary(summary)


def test_list_task_runs_operations_scope_totals_follow_filter(test_db):
    now = utc_now_for_db()
    _create_run(
        task_key="scope_scheduled_clean",
        state="completed",
        trigger_type="scheduled",
        created_at=now - timedelta(minutes=4),
    )
    scheduled_failed = _create_run(
        task_key="scope_scheduled_failed",
        state="failed",
        trigger_type="scheduled",
        created_at=now - timedelta(minutes=3),
    )
    manual_completed = _create_run(
        task_key="scope_manual_completed",
        state="completed",
        created_at=now - timedelta(minutes=2),
    )
    scheduled_pending = _create_run(
        task_key="scope_scheduled_pending",
        state="pending",
        trigger_type="scheduled",
        created_at=now - timedelta(minutes=1),
    )

    default_page = ActivityService.list_task_runs(page=1, page_size=10)
    operations_page = ActivityService.list_task_runs(page=1, page_size=10, scope="operations")
    operations_first = ActivityService.list_task_runs(page=1, page_size=1, scope="operations")
    operations_second = ActivityService.list_task_runs(
        page=2, page_size=1, scope="operations"
    )

    assert default_page.total == 4
    assert operations_page.total == 3
    assert operations_first.total == 3
    assert operations_second.total == 3
    assert {item.id for item in operations_page.items} == {
        scheduled_failed.id,
        manual_completed.id,
        scheduled_pending.id,
    }
    assert len(operations_first.items) == 1
    assert len(operations_second.items) == 1


def test_attention_uses_latest_terminal_run_per_task(test_db):
    now = utc_now_for_db()
    failed_latest = _create_run(
        task_key="attention_failed_latest",
        state="failed",
        created_at=now - timedelta(hours=6),
    )
    warned_latest = _create_run(
        task_key="attention_warned_latest",
        state="completed",
        created_at=now - timedelta(hours=2),
        result_summary={"failed_count": 3},
    )
    # 更早的失败在更新的一条干净完成之后不再算待关注。
    _create_run(
        task_key="attention_recovered",
        state="failed",
        created_at=now - timedelta(days=3),
    )
    _create_run(
        task_key="attention_recovered",
        state="completed",
        created_at=now - timedelta(hours=1),
    )
    # 只有运行中的任务没有终态行，不进入待关注。
    _create_run(
        task_key="attention_running_only",
        state="running",
        created_at=now - timedelta(minutes=10),
    )
    # 7 天窗口外的历史失败不计数。
    _create_run(
        task_key="attention_old_failure",
        state="failed",
        created_at=now - timedelta(days=30),
    )
    old_latest = _create_run(
        task_key="attention_old_failure",
        state="failed",
        created_at=now - timedelta(days=2),
    )

    items = ActivityService.list_attention_task_runs()

    assert [item.task_key for item in items] == [
        "attention_warned_latest",
        "attention_failed_latest",
        "attention_old_failure",
    ]
    assert items[0].latest_run.id == warned_latest.id
    assert items[0].latest_run.has_warning is True
    assert items[0].failed_count_7d == 0
    assert items[1].latest_run.id == failed_latest.id
    assert items[1].failed_count_7d == 1
    assert items[1].last_issue_at == failed_latest.finished_at
    assert items[2].latest_run.id == old_latest.id
    assert items[2].failed_count_7d == 1
    assert ActivityService.list_attention_task_runs(limit=1)[0].task_key == (
        "attention_warned_latest"
    )


@pytest.mark.parametrize(
    "task_key",
    [
        "download_task_sync",
        "download_task_auto_import",
        "media_thumbnail_generation",
        "image_search_index",
    ],
)
def test_frequent_task_keeps_only_latest_clean_success(test_db, task_key):
    first = ActivityService.create_task_run(
        task_key=task_key,
        trigger_type="scheduled",
    )
    ActivityService.complete_task_run(first.id, notify_result=False)
    latest = ActivityService.create_task_run(
        task_key=task_key,
        trigger_type="scheduled",
    )
    ActivityService.complete_task_run(latest.id, notify_result=False)

    assert (
        not BackgroundTaskRun.select().where(BackgroundTaskRun.id == first.id).exists()
    )
    assert BackgroundTaskRun.get_by_id(latest.id).state == "completed"


def test_success_compaction_preserves_failures_warnings_and_active_runs(test_db):
    task_key = "download_task_sync"
    warning = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(
        warning.id,
        result_summary={"failed_count": 1},
        notify_result=False,
    )
    failed = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.fail_task_run(failed.id, error_message="boom", notify_result=False)
    running = ActivityService.create_task_run(
        task_key=task_key,
        trigger_type="scheduled",
        state="running",
    )
    first_clean = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(first_clean.id, notify_result=False)
    latest_clean = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(latest_clean.id, notify_result=False)

    assert (
        not BackgroundTaskRun.select()
        .where(BackgroundTaskRun.id == first_clean.id)
        .exists()
    )
    assert BackgroundTaskRun.get_by_id(latest_clean.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(warning.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(failed.id).state == "failed"
    assert BackgroundTaskRun.get_by_id(running.id).state == "running"


def test_non_frequent_task_keeps_multiple_successes(test_db):
    task_key = "movie_heat_update"
    first = ActivityService.create_task_run(task_key=task_key, trigger_type="scheduled")
    ActivityService.complete_task_run(first.id, notify_result=False)
    second = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(second.id, notify_result=False)

    assert BackgroundTaskRun.get_by_id(first.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(second.id).state == "completed"


def test_success_compaction_detaches_notification_reference(test_db):
    task_key = "image_search_index"
    old_success = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(old_success.id, notify_result=False)
    notification = SystemNotification.create(
        category="task",
        title="旧任务通知",
        content="任务已完成",
        related_task_run=old_success.id,
    )
    latest_success = ActivityService.create_task_run(
        task_key=task_key, trigger_type="scheduled"
    )
    ActivityService.complete_task_run(latest_success.id, notify_result=False)

    assert (
        not BackgroundTaskRun.select()
        .where(BackgroundTaskRun.id == old_success.id)
        .exists()
    )
    notification = SystemNotification.get_by_id(notification.id)
    assert notification.related_task_run_id is None
