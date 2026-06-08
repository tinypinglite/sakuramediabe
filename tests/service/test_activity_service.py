from src.model import BackgroundTaskRun, SystemEvent, SystemNotification
from src.service.system import ActivityService, TaskRunConflictError


def test_activity_service_tracks_successful_task_without_creating_info_notification(test_db):
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

    assert result["success_targets"] == 2
    assert task_run.state == "completed"
    assert task_run.progress_current == 1
    assert task_run.progress_total == 2
    assert task_run.result_summary["total_targets"] == 2
    # 常态成功不再写通知，避免高频任务刷屏。
    assert SystemNotification.select().count() == 0
    assert SystemEvent.select().count() >= 2


def test_activity_service_creates_warning_notification_when_success_has_failures(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
    )
    # 成功但带 failed 统计时仍需发 warning 通知，方便前端高亮。
    ActivityService.complete_task_run(
        task_run.id,
        result_summary={"total_targets": 3, "success_targets": 2, "failed_targets": 1},
    )

    notification = SystemNotification.get()
    assert notification.category == "warning"
    assert notification.related_task_run_id == task_run.id


def test_activity_service_does_not_notify_when_success_only_has_skipped(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
    )
    # 成功且仅带 skipped 统计时不再发通知：跳过属常态，计入只会让通知中心刷屏。
    ActivityService.complete_task_run(
        task_run.id,
        result_summary={"total_targets": 3, "success_targets": 2, "skipped_targets": 1},
    )

    assert SystemNotification.select().count() == 0


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
    # 样例只展示番号，不再使用标题。
    assert "ABC-001" in notification.content
    assert "ABC-002" in notification.content
    assert "A片" not in notification.content


def test_activity_service_bootstrap_aggregates_notifications_tasks_and_cursor(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    task_run = ActivityService.create_task_run(
        task_key="ranking_sync",
        trigger_type="scheduled",
        state="running",
    )
    ActivityService._create_notification(
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
        ActivityService._create_notification(
            category="reminder",
            title="有新的影片可以播放了",
            content="新增 1 部影片",
        )
    except RuntimeError as exc:
        assert str(exc) == "event publish failed"
    else:
        raise AssertionError("expected _create_notification to fail")

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


def test_activity_service_marks_all_unread_notifications_read(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    first = ActivityService._create_notification(category="reminder", title="A", content="a")
    second = ActivityService._create_notification(category="warning", title="B", content="b")
    # 预先读掉一条，验证批量已读只处理仍未读的条目。
    ActivityService.mark_notification_read(first.id)

    response = ActivityService.mark_all_notifications_read()

    assert response.updated_count == 1
    assert response.unread_count == 0
    assert ActivityService.get_unread_count() == 0
    second_db = SystemNotification.get_by_id(second.id)
    assert second_db.is_read is True
    assert second_db.read_at is not None
    # 批量已读只发一条聚合事件，并带上本次处理条数。
    read_all_events = list(
        SystemEvent.select().where(SystemEvent.event_type == "notifications_read_all")
    )
    assert len(read_all_events) == 1
    assert read_all_events[0].payload["updated_count"] == 1


def test_activity_service_mark_all_read_is_noop_without_unread(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    response = ActivityService.mark_all_notifications_read()

    assert response.updated_count == 0
    assert response.unread_count == 0
    # 没有未读时不应产生聚合事件。
    assert (
        SystemEvent.select().where(SystemEvent.event_type == "notifications_read_all").count()
        == 0
    )


def test_activity_service_marks_selected_notifications_read(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    first = ActivityService._create_notification(category="reminder", title="A", content="a")
    second = ActivityService._create_notification(category="warning", title="B", content="b")
    third = ActivityService._create_notification(category="error", title="C", content="c")

    response = ActivityService.mark_notifications_read([first.id, second.id])

    assert response.updated_count == 2
    assert response.unread_count == 1
    assert SystemNotification.get_by_id(first.id).is_read is True
    assert SystemNotification.get_by_id(second.id).is_read is True
    # 未在 ID 列表中的通知保持未读。
    assert SystemNotification.get_by_id(third.id).is_read is False
    # 按 ID 批量已读发一条带目标 ID 的聚合事件。
    read_events = list(
        SystemEvent.select().where(SystemEvent.event_type == "notifications_read")
    )
    assert len(read_events) == 1
    assert read_events[0].payload["updated_count"] == 2
    assert read_events[0].payload["ids"] == [first.id, second.id]


def test_activity_service_mark_notifications_read_ignores_empty_and_unknown_ids(test_db):
    models = [BackgroundTaskRun, SystemNotification, SystemEvent]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)

    notification = ActivityService._create_notification(category="reminder", title="A", content="a")

    # 空列表是良定义的 no-op，不报错也不发事件。
    empty_response = ActivityService.mark_notifications_read([])
    assert empty_response.updated_count == 0
    assert empty_response.unread_count == 1

    # 不存在的 ID 被忽略，仅统计真正更新的条数。
    mixed_response = ActivityService.mark_notifications_read([notification.id, 999999])
    assert mixed_response.updated_count == 1
    assert mixed_response.unread_count == 0
    assert (
        SystemEvent.select().where(SystemEvent.event_type == "notifications_read").count() == 1
    )
