from src.model import BackgroundTaskRun, SystemEvent, SystemNotification
from src.service.system import ActivityService, TaskRunConflictError


def test_activity_service_tracks_successful_task_and_creates_result_notification(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    result = ActivityService.run_task(
        task_key="ranking_sync",
        trigger_type="scheduled",
        func=lambda reporter: (
            reporter.emit(
                current=1,
                total=2,
                text="已同步第一个榜单",
                summary_patch={"success_targets": 1},
            ),
            {"total_targets": 2, "success_targets": 2, "failed_targets": 0},
        )[1],
    )

    task_run = BackgroundTaskRun.get()
    notification = SystemNotification.get()

    assert result["success_targets"] == 2
    assert task_run.state == "completed"
    assert task_run.progress_current == 1
    assert task_run.progress_total == 2
    assert task_run.result_summary["total_targets"] == 2
    assert notification.category == "info"
    assert notification.related_task_run_id == task_run.id
    assert SystemEvent.select().count() >= 2


def test_activity_service_resolves_movie_interaction_sync_task_name(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="movie_interaction_sync",
        trigger_type="scheduled",
    )

    assert task_run.task_name == "影片互动数同步"


def test_activity_service_resolves_movie_similarity_recompute_task_name(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="movie_similarity_recompute",
        trigger_type="scheduled",
    )

    assert task_run.task_name == "影片相似度重算"


def test_activity_service_marks_failure_and_creates_exception_notification(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    try:
        ActivityService.run_task(
            task_key="image_search_index",
            trigger_type="scheduled",
            func=lambda reporter: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    except RuntimeError:
        pass

    task_run = BackgroundTaskRun.get()
    notification = SystemNotification.get()

    assert task_run.state == "failed"
    assert task_run.error_message == "boom"
    assert notification.category == "error"


def test_activity_service_rejects_duplicate_mutex_key_when_conflict_policy_is_raise(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    first_task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
        mutex_key="aps:ranking_sync",
    )

    try:
        ActivityService.run_task(
            task_key="ranking_sync",
            trigger_type="manual",
            func=lambda reporter: {"ok": True},
            mutex_key="aps:ranking_sync",
            conflict_policy="raise",
        )
    except TaskRunConflictError as exc:
        assert exc.blocking_task_run.id == first_task_run.id
        assert "任务“排行榜同步”已在运行中" in str(exc)
    else:
        raise AssertionError("expected TaskRunConflictError")

    assert BackgroundTaskRun.select().count() == 1


def test_activity_service_skips_duplicate_mutex_key_when_conflict_policy_is_skip(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    first_task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="manual",
        state="running",
        mutex_key="aps:ranking_sync",
    )
    called = {"func": 0}

    result = ActivityService.run_task(
        task_key="ranking_sync",
        trigger_type="scheduled",
        func=lambda reporter: called.__setitem__("func", called["func"] + 1),
        mutex_key="aps:ranking_sync",
        conflict_policy="skip",
    )

    assert result == {
        "task_skipped": True,
        "reason": "mutex_conflict",
        "blocking_task_run_id": first_task_run.id,
        "blocking_task_key": "ranking_sync",
        "blocking_trigger_type": "manual",
        "blocking_started_at": first_task_run.started_at.isoformat(),
        "blocking_task_name": "排行榜同步",
    }
    assert called["func"] == 0
    assert BackgroundTaskRun.select().count() == 1


def test_activity_service_clears_mutex_key_after_completion_and_failure(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    completed_task_run = ActivityService.run_task(
        task_key="ranking_sync",
        trigger_type="scheduled",
        func=lambda reporter: {"total_targets": 1},
        mutex_key="aps:ranking_sync",
    )
    assert completed_task_run["total_targets"] == 1

    first_task_run = BackgroundTaskRun.get_by_id(1)
    assert first_task_run.state == "completed"
    assert first_task_run.mutex_key is None

    try:
        ActivityService.run_task(
            task_key="ranking_sync",
            trigger_type="scheduled",
            func=lambda reporter: (_ for _ in ()).throw(RuntimeError("boom")),
            mutex_key="aps:ranking_sync",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")

    failed_task_run = BackgroundTaskRun.get_by_id(2)
    assert failed_task_run.state == "failed"
    assert failed_task_run.mutex_key is None

def test_activity_service_creates_deduplicated_media_reminder(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    notification = ActivityService.create_new_media_reminder(
        movie_items=[
            {"movie_id": 1, "movie_number": "ABC-001", "title": "A片 1"},
            {"movie_id": 1, "movie_number": "ABC-001", "title": "A片 1"},
            {"movie_id": 2, "movie_number": "ABC-002", "title": "A片 2"},
        ]
    )

    assert notification is not None
    assert notification.category == "reminder"
    assert "新增可播放影片 2 部" in notification.content


def test_activity_service_bootstrap_aggregates_notifications_tasks_and_cursor(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
    )
    ActivityService.create_notification(
        category="reminder",
        title="有新的影片可以播放了",
        content="新增 1 部影片",
    )

    bootstrap = ActivityService.get_activity_bootstrap(
        notification_category="reminder",
        task_state="running",
    )

    assert bootstrap.latest_event_id == SystemEvent.select().order_by(SystemEvent.id.desc()).get().id
    assert bootstrap.notifications.total == 1
    assert bootstrap.notifications.items[0].category == "reminder"
    assert bootstrap.unread_count == 1
    assert len(bootstrap.active_task_runs) == 1
    assert bootstrap.active_task_runs[0].id == task_run.id
    assert bootstrap.task_runs.total == 1
    assert bootstrap.task_runs.items[0].id == task_run.id


def test_activity_service_rolls_back_notification_when_event_publish_fails(test_db, monkeypatch):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    def fake_publish(**kwargs):
        raise RuntimeError("event publish failed")

    monkeypatch.setattr("src.service.system.activity_service.SystemEventService.publish", fake_publish)

    try:
        ActivityService.create_notification(
            category="reminder",
            title="有新的影片可以播放了",
            content="新增 1 部影片",
        )
    except RuntimeError as exc:
        assert str(exc) == "event publish failed"
    else:
        raise AssertionError("expected create_notification to fail")

    assert SystemNotification.select().count() == 0
    assert SystemEvent.select().count() == 0


def test_activity_service_rolls_back_task_state_when_event_publish_fails(test_db, monkeypatch):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="pending",
    )

    def fake_publish(**kwargs):
        raise RuntimeError("event publish failed")

    monkeypatch.setattr("src.service.system.activity_service.SystemEventService.publish", fake_publish)

    try:
        ActivityService.mark_task_run_running(task_run.id)
    except RuntimeError as exc:
        assert str(exc) == "event publish failed"
    else:
        raise AssertionError("expected mark_task_run_running to fail")

    task_run = BackgroundTaskRun.get_by_id(task_run.id)
    assert task_run.state == "pending"
    assert task_run.started_at is None


def test_activity_service_recovers_interrupted_scheduled_tasks_without_touching_other_triggers(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    scheduled_running = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
        owner_pid=999999,
        mutex_key="aps:ranking_sync",
    )
    scheduled_pending = ActivityService.create_task_run(
        task_key="movie_heat_update",
        trigger_type="scheduled",
        state="pending",
        owner_pid=999999,
        mutex_key="aps:movie_heat_update",
    )
    startup_running = ActivityService.create_task_run(
        task_key="legacy_startup_task",
        trigger_type="startup",
        state="running",
    )
    manual_running = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="manual",
        state="running",
    )

    recovered = ActivityService.recover_interrupted_task_runs(
        trigger_type="scheduled",
        error_message="APS进程重启，任务已中断",
    )

    scheduled_running = BackgroundTaskRun.get_by_id(scheduled_running.id)
    scheduled_pending = BackgroundTaskRun.get_by_id(scheduled_pending.id)
    startup_running = BackgroundTaskRun.get_by_id(startup_running.id)
    manual_running = BackgroundTaskRun.get_by_id(manual_running.id)

    assert [task_run.id for task_run in recovered] == [scheduled_running.id, scheduled_pending.id]
    assert scheduled_running.state == "failed"
    assert scheduled_pending.state == "failed"
    assert scheduled_running.mutex_key is None
    assert scheduled_pending.mutex_key is None
    assert scheduled_running.finished_at is not None
    assert scheduled_pending.finished_at is not None
    assert scheduled_running.error_message == "APS进程重启，任务已中断"
    assert scheduled_pending.error_message == "APS进程重启，任务已中断"
    assert startup_running.state == "running"
    assert manual_running.state == "running"
    assert (
        SystemNotification.select()
        .where(SystemNotification.category == "error")
        .count()
        == 2
    )


def test_activity_service_recovers_interrupted_startup_tasks_without_touching_completed_or_internal(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    startup_running = ActivityService.create_task_run(
        task_key="legacy_startup_task",
        trigger_type="startup",
        state="running",
        owner_pid=999999,
    )
    startup_completed = ActivityService.create_task_run(
        task_key="legacy_startup_task",
        trigger_type="startup",
        state="pending",
        owner_pid=999999,
    )
    ActivityService.complete_task_run(startup_completed.id, result_summary={"updated_media": 1})
    internal_running = ActivityService.create_task_run(
        task_key="download_task_import",
        trigger_type="internal",
        state="running",
    )

    recovered = ActivityService.recover_interrupted_task_runs(
        trigger_type="startup",
        error_message="API进程重启，任务已中断",
    )

    startup_running = BackgroundTaskRun.get_by_id(startup_running.id)
    startup_completed = BackgroundTaskRun.get_by_id(startup_completed.id)
    internal_running = BackgroundTaskRun.get_by_id(internal_running.id)

    assert [task_run.id for task_run in recovered] == [startup_running.id]
    assert startup_running.state == "failed"
    assert startup_running.error_message == "API进程重启，任务已中断"
    assert startup_completed.state == "completed"
    assert internal_running.state == "running"


def test_activity_service_does_not_recover_task_run_when_owner_process_is_alive(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
    )

    recovered = ActivityService.recover_interrupted_task_runs(
        trigger_type="scheduled",
        error_message="APS进程重启，任务已中断",
    )

    task_run = BackgroundTaskRun.get_by_id(task_run.id)
    assert recovered == []
    assert task_run.state == "running"
