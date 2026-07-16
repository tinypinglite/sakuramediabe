from datetime import timedelta

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, SystemEvent, SystemNotification
from src.service.system import ActivityCleanupService

_ACTIVITY_MODELS = [BackgroundTaskRun, SystemNotification, SystemEvent]


@pytest.fixture(autouse=True)
def activity_tables(test_db):
    test_db.bind(_ACTIVITY_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_ACTIVITY_MODELS)
    yield test_db


def _make_task_run(task_key: str) -> BackgroundTaskRun:
    return BackgroundTaskRun.create(
        task_key=task_key,
        task_name=task_key,
        trigger_type="scheduled",
        state="completed",
    )


def test_cleanup_system_events_keeps_only_recent_days():
    now = utc_now_for_db()
    fresh = SystemEvent.create(event_type="task_run_updated", resource_type="task_run", resource_id=1)
    stale = SystemEvent.create(event_type="task_run_updated", resource_type="task_run", resource_id=2)
    # 把旧事件的 emitted_at 改到保留窗口之外，等待被清理。
    SystemEvent.update(emitted_at=now - timedelta(days=2)).where(SystemEvent.id == stale.id).execute()

    deleted = ActivityCleanupService()._cleanup_system_events(retention_days=1)

    assert deleted == 1
    assert {event.id for event in SystemEvent.select()} == {fresh.id}


def test_cleanup_task_runs_keeps_latest_n_per_key():
    alpha_ids = [_make_task_run("alpha").id for _ in range(5)]
    beta_id = _make_task_run("beta").id

    deleted = ActivityCleanupService()._cleanup_task_runs(retention_per_key=2)

    # alpha 有 5 条，保留最近 2 条，删 3 条；beta 不足 2 条，全部保留。
    assert deleted == 3
    alpha_remaining = sorted(
        row.id for row in BackgroundTaskRun.select().where(BackgroundTaskRun.task_key == "alpha")
    )
    assert alpha_remaining == sorted(alpha_ids[-2:])
    assert BackgroundTaskRun.select().where(BackgroundTaskRun.task_key == "beta").count() == 1
    assert beta_id in {row.id for row in BackgroundTaskRun.select()}


def test_cleanup_task_runs_nulls_related_notification_fk():
    old_run = _make_task_run("alpha")
    _make_task_run("alpha")  # 较新的运行记录，触发对 old_run 的清理
    notification = SystemNotification.create(
        category="info",
        title="任务完成",
        content="详情",
        related_task_run=old_run,
    )

    ActivityCleanupService()._cleanup_task_runs(retention_per_key=1)

    # 旧运行记录被删除，关联通知的外键被显式置空而非留下悬挂引用。
    assert BackgroundTaskRun.select().where(BackgroundTaskRun.id == old_run.id).count() == 0
    refreshed = SystemNotification.get_by_id(notification.id)
    assert refreshed.related_task_run is None


def test_cleanup_notifications_removes_read_beyond_retention_only():
    now = utc_now_for_db()
    read_old = SystemNotification.create(
        category="info", title="t", content="c", is_read=True, read_at=now - timedelta(days=5)
    )
    read_recent = SystemNotification.create(
        category="info", title="t", content="c", is_read=True, read_at=now - timedelta(days=1)
    )
    unread = SystemNotification.create(category="info", title="t", content="c", is_read=False)

    deleted = ActivityCleanupService()._cleanup_notifications(retention_days=3)

    # 只删已读且超过保留期的，已读但较新的与未读的都保留。
    assert deleted == 1
    assert {n.id for n in SystemNotification.select()} == {read_recent.id, unread.id}
    assert read_old.id not in {n.id for n in SystemNotification.select()}


def test_cleanup_returns_combined_stats(monkeypatch):
    now = utc_now_for_db()
    stale_event = SystemEvent.create(event_type="task_run_updated")
    SystemEvent.update(emitted_at=now - timedelta(days=2)).where(SystemEvent.id == stale_event.id).execute()
    for _ in range(3):
        _make_task_run("alpha")
    SystemNotification.create(
        category="info", title="t", content="c", is_read=True, read_at=now - timedelta(days=5)
    )

    monkeypatch.setattr("src.config.config.settings.scheduler.activity_event_retention_days", 1)
    monkeypatch.setattr("src.config.config.settings.scheduler.activity_task_run_retention_per_key", 1)
    monkeypatch.setattr("src.config.config.settings.scheduler.activity_notification_read_retention_days", 3)

    stats = ActivityCleanupService().cleanup()

    assert stats == {"deleted_events": 1, "deleted_task_runs": 2, "deleted_notifications": 1}
