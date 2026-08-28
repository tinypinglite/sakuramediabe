from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from src.scheduler.logging import _TASK_LEVELS, _TASK_SINKS, get_task_logger
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.service.system import TaskRunConflictError
from src.start.aps import (
    _bootstrap_gfriends_filetree_refresh,
    _bootstrap_movie_similarity_index,
    _schedule_bootstrap_job,
    build_scheduler,
    enqueue_scheduled_job,
    run_job,
)
from src.start.commands import main


@pytest.fixture(autouse=True)
def _patch_command_database_prepare(monkeypatch):
    # aps 子命令测试只验证命令编排，不触发真实建表流程。
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)


def test_aps_command_invokes_scheduler_entrypoint(monkeypatch):
    called = {"aps": 0}

    def fake_aps():
        called["aps"] += 1

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.aps", fake_aps)

    result = runner.invoke(main, ["aps"])

    assert result.exit_code == 0
    assert called["aps"] == 1


# ---------------------------------------------------------------------------
# CLI 命令测试: 统一 mock queue submission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli_name",
    [
        "sync-subscribed-actor-movies",
        "update-movie-heat",
        "sync-movie-interactions",
        "sync-hot-reviews",
        "generate-media-thumbnails",
        "cleanup-activity-records",
        "index-image-search-thumbnails",
        "optimize-image-search-index",
        "recompute-movie-similarities",
        "generate-daily-recommendations",
        "generate-moment-recommendations",
        "auto-download-subscribed-movies",
    ],
)
def test_aps_cli_commands_run_job(monkeypatch, cli_name):
    called = {"job": 0}

    def fake_run_job(job_def, *, trigger_type="scheduled", params=None):
        called["job"] += 1
        assert trigger_type == "manual"
        return type("TaskRun", (), {"id": 7, "state": "pending"})()

    monkeypatch.setattr("src.start.aps.run_job", fake_run_job)

    runner = CliRunner()
    result = runner.invoke(main, ["aps", cli_name])

    assert result.exit_code == 0, result.output
    assert called["job"] == 1
    assert "task_run_id=7 state=pending" in result.output


def test_dynamic_aps_cli_validates_parameterized_handler(monkeypatch):
    import click
    from pydantic import BaseModel

    from src.scheduler.contracts import JobDefinition
    from src.start import commands as commands_module

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_cli_mixed",
        log_name="demo-cli-mixed",
        cli_name="demo-cli-mixed",
        cli_help="CLI mixed",
        default_cron="0 5 * * *",
        params_schema=EmptyParams,
        handler=lambda reporter, params: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    group = click.Group()
    captured = []
    monkeypatch.setattr(
        commands_module,
        "_run_cli_job",
        lambda received_job, params=None: captured.append((received_job, params)),
    )
    commands_module._register_aps_command(job_def, group)
    runner = CliRunner()

    omitted = runner.invoke(group, [job_def.cli_name])
    explicit_null = runner.invoke(group, [job_def.cli_name, "--params-json", "null"])
    explicit_empty = runner.invoke(
        group,
        [job_def.cli_name, "--params-json", "{}"],
    )

    assert omitted.exit_code == 0, omitted.output
    assert explicit_null.exit_code == 0, explicit_null.output
    assert explicit_empty.exit_code == 0, explicit_empty.output
    assert captured == [(job_def, None), (job_def, None), (job_def, {})]
    assert job_def.cli_name in group.commands


def test_dynamic_aps_cli_manual_handler_requires_params(monkeypatch):
    import click
    from pydantic import BaseModel

    from src.scheduler.contracts import JobDefinition
    from src.start import commands as commands_module

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_cli_handler_only",
        log_name="demo-cli-handler-only",
        cli_name="demo-cli-handler-only",
        cli_help="CLI handler only",
        manual_only=True,
        params_schema=EmptyParams,
        handler=lambda reporter, params: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    group = click.Group()
    captured = []
    monkeypatch.setattr(
        commands_module,
        "_run_cli_job",
        lambda received_job, params=None: captured.append((received_job, params)),
    )
    commands_module._register_aps_command(job_def, group)

    result = CliRunner().invoke(group, [job_def.cli_name])
    explicit_null = CliRunner().invoke(
        group,
        [job_def.cli_name, "--params-json", "null"],
    )

    assert result.exit_code != 0
    assert "Missing option '--params-json'" in result.output
    assert explicit_null.exit_code != 0
    assert "参数不能为 JSON null" in explicit_null.output
    assert captured == []
    assert job_def.cli_name in group.commands


def test_aps_subcommand_prepares_database_before_running_job(monkeypatch):
    events = []

    def fake_prepare_database():
        events.append("db.ready")

    def fake_run_job(job_def, *, trigger_type="scheduled", params=None):
        events.append(("job", trigger_type))
        return type("TaskRun", (), {"id": 7, "state": "pending"})()

    runner = CliRunner()
    monkeypatch.setattr("src.start.commands._ensure_database_ready", fake_prepare_database)
    monkeypatch.setattr("src.start.aps.run_job", fake_run_job)

    result = runner.invoke(main, ["aps", "update-movie-heat"])

    assert result.exit_code == 0
    assert events == ["db.ready", ("job", "manual")]


def test_aps_manual_subcommand_exits_with_click_error_when_task_conflicts(monkeypatch):
    runner = CliRunner()
    blocking_task_run = type(
        "TaskRun",
        (),
        {
            "id": 9,
            "task_key": "actor_subscription_sync",
            "task_name": "订阅演员影片同步",
            "trigger_type": "scheduled",
            "started_at": None,
        },
    )()
    monkeypatch.setattr(
        "src.start.aps.run_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(TaskRunConflictError(blocking_task_run)),
    )

    result = runner.invoke(main, ["aps", "sync-subscribed-actor-movies"])

    assert result.exit_code != 0
    assert "任务“订阅演员影片同步”已在运行中" in result.output


# ---------------------------------------------------------------------------
# build_scheduler 测试
# ---------------------------------------------------------------------------


def test_build_scheduler_registers_all_jobs(monkeypatch):
    monkeypatch.setattr("src.start.aps.get_runtime_timezone", lambda: ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.start.aps.get_runtime_timezone_name", lambda: "Asia/Shanghai")
    monkeypatch.setattr("src.start.aps.settings.scheduler.actor_subscription_sync_cron", "0 2 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.subscribed_movie_auto_download_cron", "30 2 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_heat_cron", "15 0 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.download_task_sync_cron", "*/15 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.download_task_auto_import_cron", "*/10 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_interaction_sync_cron", "0 5 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_thumbnail_cron", "*/5 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_index_cron", "*/10 * * * *")
    monkeypatch.setattr(
        "src.start.aps.settings.scheduler.plot_image_search_index_cron", "30 0 * * *"
    )
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_optimize_cron", "0 */6 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_similarity_recompute_cron", "30 3 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.moment_recommendation_generate_cron", "0 4 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.daily_recommendation_generate_cron", "0 5 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.hot_review_sync_cron", "20 1 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.activity_cleanup_cron", "30 5 * * *")

    scheduler = build_scheduler()

    # 验证所有任务都已注册
    for job_def in JOB_REGISTRY:
        job = scheduler.get_job(job_def.task_key)
        assert job is not None, f"Job {job_def.task_key} not registered"

    # 验证部分 cron 表达式
    assert str(scheduler.get_job("actor_subscription_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='2', minute='0']"
    assert str(scheduler.get_job("subscribed_movie_auto_download").trigger) == "cron[month='*', day='*', day_of_week='*', hour='2', minute='30']"
    assert str(scheduler.get_job("hot_review_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='20']"
    assert str(scheduler.get_job("movie_interaction_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='5', minute='0']"
    assert str(scheduler.get_job("movie_heat_update").trigger) == "cron[month='*', day='*', day_of_week='*', hour='0', minute='15']"
    assert str(scheduler.get_job("download_task_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/15']"
    assert str(scheduler.get_job("download_task_auto_import").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(scheduler.get_job("media_thumbnail_generation").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/5']"
    assert str(scheduler.get_job("image_search_index").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert (
        str(scheduler.get_job("plot_image_search_index").trigger)
        == "cron[month='*', day='*', day_of_week='*', hour='0', minute='30']"
    )
    assert str(scheduler.get_job("image_search_optimize").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*/6', minute='0']"
    assert str(scheduler.get_job("movie_similarity_recompute").trigger) == "cron[month='*', day='*', day_of_week='*', hour='3', minute='30']"
    assert str(scheduler.get_job("moment_recommendation_generate").trigger) == "cron[month='*', day='*', day_of_week='*', hour='4', minute='0']"
    assert str(scheduler.get_job("daily_recommendation_generate").trigger) == "cron[month='*', day='*', day_of_week='*', hour='5', minute='0']"
    assert str(scheduler.get_job("activity_record_cleanup").trigger) == "cron[month='*', day='*', day_of_week='*', hour='5', minute='30']"
    assert scheduler.timezone.key == "Asia/Shanghai"


def test_bootstrap_movie_similarity_index_schedules_missing_alias(monkeypatch):
    scheduler = build_scheduler()
    monkeypatch.setattr(
        "src.service.discovery.qdrant_movie_similarity_store."
        "get_qdrant_movie_similarity_store",
        lambda: type("Store", (), {"is_ready": lambda self: False})(),
    )

    _bootstrap_movie_similarity_index(scheduler)

    job = scheduler.get_job("bootstrap_movie_similarity_index")
    assert job is not None
    assert job.trigger.run_date is not None


def test_similarity_bootstrap_enqueues_startup_run_and_worker_executes_handler(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    job_def = JOB_REGISTRY_BY_KEY["movie_similarity_recompute"]
    fake_def = job_def.model_copy(
        update={"handler": lambda _reporter, _params: calls.append("handler") or {}}
    )
    monkeypatch.setitem(
        JOB_REGISTRY_BY_KEY,
        "movie_similarity_recompute",
        fake_def,
    )
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        "movie_similarity_recompute",
        job_id="test_bootstrap_movie_similarity",
    )

    bootstrap_job = scheduler.get_job("test_bootstrap_movie_similarity")
    bootstrap_job.func()
    bootstrap_job.func()

    queued_runs = list(
        BackgroundTaskRun.select().where(
            BackgroundTaskRun.task_key == "movie_similarity_recompute"
        )
    )
    assert calls == []
    assert len(queued_runs) == 1
    queued = queued_runs[0]
    assert queued.state == "pending"
    assert queued.trigger_type == "startup"
    assert queued.params is None
    assert queued.scheduled_at is not None
    assert queued.mutex_key == "aps:movie_similarity_recompute"

    TaskWorker()._execute(TaskQueueService.claim_next())

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert calls == ["handler"]
    assert stored.state == "completed"
    assert stored.mutex_key is None


def test_gfriends_bootstrap_explicit_params_preserve_force_false_and_cron_null_handler(
    test_db, monkeypatch, tmp_path
):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []

    def fake_refresh_gfriends_filetree(*, force):
        calls.append(force)
        return {"entries": 1}

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.aps.settings.metadata.gfriends_filetree_cache_path",
        str(tmp_path / "missing-gfriends-filetree.json"),
    )
    monkeypatch.setattr(
        "src.metadata.factory.refresh_gfriends_filetree",
        fake_refresh_gfriends_filetree,
    )
    scheduler = build_scheduler()
    _bootstrap_gfriends_filetree_refresh(scheduler)

    bootstrap_job = scheduler.get_job("bootstrap_gfriends_filetree_refresh")
    bootstrap_job.func()

    startup = BackgroundTaskRun.get(
        BackgroundTaskRun.task_key == "gfriends_filetree_refresh"
    )
    assert calls == []
    assert startup.trigger_type == "startup"
    assert startup.state == "pending"
    assert startup.params == {"force": False}
    assert startup.scheduled_at is not None

    TaskWorker()._execute(TaskQueueService.claim_next())
    assert calls == [False]
    assert BackgroundTaskRun.get_by_id(startup.id).mutex_key is None

    scheduled = enqueue_scheduled_job(JOB_REGISTRY_BY_KEY["gfriends_filetree_refresh"])
    assert scheduled is not None
    assert scheduled.params is None
    TaskWorker()._execute(TaskQueueService.claim_next())

    assert calls == [False, True]
    assert BackgroundTaskRun.get_by_id(scheduled.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(scheduled.id).mutex_key is None


def test_bootstrap_recovers_expired_running_blocker_and_executes_replacement(
    test_db, monkeypatch
):
    from datetime import timedelta

    from src.common.runtime_time import utc_now_for_db
    from src.model import BackgroundTaskRun, SystemNotification
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import (
        BOOTSTRAP_LEASE_EXPIRED_ERROR_MESSAGE,
        FAILURE_CODE_QUEUE_LEASE_EXPIRED,
        INTERNAL_FAILURE_CODE_KEY,
        TaskQueueService,
    )

    calls = []
    job_def = JOB_REGISTRY_BY_KEY["movie_similarity_recompute"]
    monkeypatch.setitem(
        JOB_REGISTRY_BY_KEY,
        job_def.task_key,
        job_def.model_copy(
            update={"handler": lambda _reporter, _params: calls.append("handler") or {}}
        ),
    )
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    stale = TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="startup",
    )
    TaskQueueService.claim_next(lease_seconds=60)
    BackgroundTaskRun.update(
        lease_expires_at=utc_now_for_db() - timedelta(seconds=1)
    ).where(BackgroundTaskRun.id == stale.id).execute()

    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        job_def.task_key,
        job_id="test_expired_bootstrap",
    )
    scheduler.get_job("test_expired_bootstrap").func()

    runs = list(
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.task_key == job_def.task_key)
        .order_by(BackgroundTaskRun.id)
    )
    assert len(runs) == 2
    recovered, replacement = runs
    assert recovered.id == stale.id
    assert recovered.state == "failed"
    assert recovered.error_message == BOOTSTRAP_LEASE_EXPIRED_ERROR_MESSAGE
    assert (
        recovered.result_summary[INTERNAL_FAILURE_CODE_KEY]
        == FAILURE_CODE_QUEUE_LEASE_EXPIRED
    )
    assert recovered.mutex_key is None
    assert replacement.state == "pending"
    assert replacement.trigger_type == "startup"
    assert replacement.params is None
    assert replacement.mutex_key == f"aps:{job_def.task_key}"
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == recovered.id
    ).count() == 1

    TaskWorker()._execute(TaskQueueService.claim_next())
    guard = scheduler.get_job("test_expired_bootstrap_completion_guard")
    guard.func(*guard.args, **guard.kwargs)

    assert calls == ["handler"]
    assert BackgroundTaskRun.get_by_id(replacement.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(replacement.id).mutex_key is None
    assert BackgroundTaskRun.select().where(
        BackgroundTaskRun.task_key == job_def.task_key
    ).count() == 2
    # 过期回收只发一条失败通知；正常成功按现有通知策略不额外提醒。
    assert SystemNotification.select().where(
        SystemNotification.related_task_run.in_([recovered.id, replacement.id])
    ).count() == 1


def test_bootstrap_guard_does_not_duplicate_healthy_running_blocker(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun, SystemNotification
    from src.service.system.task_queue_service import TaskQueueService

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    task_key = "movie_similarity_recompute"
    healthy = TaskQueueService.enqueue(task_key=task_key, trigger_type="startup")
    TaskQueueService.claim_next(lease_seconds=3600)
    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        task_key,
        job_id="test_healthy_bootstrap",
    )

    scheduler.get_job("test_healthy_bootstrap").func()
    first_guard = scheduler.get_job("test_healthy_bootstrap_completion_guard")
    first_guard.func(*first_guard.args, **first_guard.kwargs)

    stored = BackgroundTaskRun.get_by_id(healthy.id)
    assert stored.state == "running"
    assert stored.mutex_key == f"aps:{task_key}"
    assert BackgroundTaskRun.select().where(
        BackgroundTaskRun.task_key == task_key
    ).count() == 1
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == healthy.id
    ).count() == 0
    assert scheduler.get_job("test_healthy_bootstrap_completion_guard") is not None


def test_bootstrap_pending_blocker_executes_once_and_completed_guard_stops(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun, SystemNotification
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    task_key = "movie_similarity_recompute"
    job_def = JOB_REGISTRY_BY_KEY[task_key]
    monkeypatch.setitem(
        JOB_REGISTRY_BY_KEY,
        task_key,
        job_def.model_copy(
            update={"handler": lambda _reporter, _params: calls.append("handler") or {}}
        ),
    )
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    pending = TaskQueueService.enqueue(task_key=task_key, trigger_type="startup")
    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        task_key,
        job_id="test_pending_bootstrap",
    )

    scheduler.get_job("test_pending_bootstrap").func()
    assert BackgroundTaskRun.select().where(
        BackgroundTaskRun.task_key == task_key
    ).count() == 1

    TaskWorker()._execute(TaskQueueService.claim_next())
    guard = scheduler.get_job("test_pending_bootstrap_completion_guard")
    guard.func(*guard.args, **guard.kwargs)

    stored = BackgroundTaskRun.get_by_id(pending.id)
    assert calls == ["handler"]
    assert stored.state == "completed"
    assert stored.mutex_key is None
    assert BackgroundTaskRun.select().where(
        BackgroundTaskRun.task_key == task_key
    ).count() == 1
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == pending.id
    ).count() == 0
    assert scheduler.get_job("test_pending_bootstrap_completion_guard") is None


def test_bootstrap_guard_requeues_run_failed_by_lease_housekeeper(
    test_db, monkeypatch
):
    from datetime import timedelta

    from src.common.runtime_time import utc_now_for_db
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import (
        FAILURE_CODE_QUEUE_LEASE_EXPIRED,
        INTERNAL_FAILURE_CODE_KEY,
        TaskQueueService,
    )

    calls = []
    task_key = "movie_similarity_recompute"
    job_def = JOB_REGISTRY_BY_KEY[task_key]
    monkeypatch.setitem(
        JOB_REGISTRY_BY_KEY,
        task_key,
        job_def.model_copy(
            update={"handler": lambda _reporter, _params: calls.append("handler") or {}}
        ),
    )
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    interrupted = TaskQueueService.enqueue(task_key=task_key, trigger_type="startup")
    TaskQueueService.claim_next(lease_seconds=3600)
    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        task_key,
        job_id="test_housekeeper_bootstrap",
    )
    scheduler.get_job("test_housekeeper_bootstrap").func()
    BackgroundTaskRun.update(
        lease_expires_at=utc_now_for_db() - timedelta(seconds=1)
    ).where(BackgroundTaskRun.id == interrupted.id).execute()
    recovered = TaskQueueService.recover_expired_leases(
        error_message="租约提示文案已经变化"
    )
    assert [run.id for run in recovered] == [interrupted.id]
    assert (
        BackgroundTaskRun.get_by_id(interrupted.id).result_summary[
            INTERNAL_FAILURE_CODE_KEY
        ]
        == FAILURE_CODE_QUEUE_LEASE_EXPIRED
    )

    guard = scheduler.get_job("test_housekeeper_bootstrap_completion_guard")
    guard.func(*guard.args, **guard.kwargs)

    replacement = (
        BackgroundTaskRun.select()
        .where(
            BackgroundTaskRun.task_key == task_key,
            BackgroundTaskRun.id != interrupted.id,
        )
        .get()
    )
    assert replacement.state == "pending"
    assert replacement.mutex_key == f"aps:{task_key}"
    TaskWorker()._execute(TaskQueueService.claim_next())
    assert calls == ["handler"]
    assert BackgroundTaskRun.get_by_id(replacement.id).state == "completed"


def test_bootstrap_guard_does_not_requeue_business_failure_with_lease_message(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun, SystemNotification
    from src.service.system import ActivityService
    from src.service.system.task_queue_service import (
        LEASE_EXPIRED_ERROR_MESSAGE,
        TaskQueueService,
    )

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    task_key = "movie_similarity_recompute"
    running = TaskQueueService.enqueue(task_key=task_key, trigger_type="startup")
    TaskQueueService.claim_next(lease_seconds=3600)
    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        task_key,
        job_id="test_business_failed_bootstrap",
    )
    scheduler.get_job("test_business_failed_bootstrap").func()
    ActivityService.fail_task_run(
        running.id,
        error_message=LEASE_EXPIRED_ERROR_MESSAGE,
    )

    guard = scheduler.get_job("test_business_failed_bootstrap_completion_guard")
    guard.func(*guard.args, **guard.kwargs)

    stored = BackgroundTaskRun.get_by_id(running.id)
    assert stored.state == "failed"
    assert stored.result_summary == {}
    assert BackgroundTaskRun.select().where(
        BackgroundTaskRun.task_key == task_key
    ).count() == 1
    assert SystemNotification.select().where(
        SystemNotification.related_task_run == running.id
    ).count() == 1
    assert scheduler.get_job("test_business_failed_bootstrap_completion_guard") is None


@pytest.mark.parametrize("failure_point", ["database", "enqueue", "settle"])
def test_bootstrap_transient_failure_schedules_retry_and_eventually_completes(
    test_db, monkeypatch, failure_point
):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    task_key = "movie_similarity_recompute"
    calls = []
    job_def = JOB_REGISTRY_BY_KEY[task_key]
    monkeypatch.setitem(
        JOB_REGISTRY_BY_KEY,
        task_key,
        job_def.model_copy(
            update={"handler": lambda _reporter, _params: calls.append("handler") or {}}
        ),
    )
    attempts = {"count": 0}
    if failure_point == "settle":
        TaskQueueService.enqueue(task_key=task_key, trigger_type="startup")

    if failure_point == "database":
        def flaky_database_ready():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("database unavailable")

        monkeypatch.setattr("src.start.aps.ensure_database_ready", flaky_database_ready)
    else:
        monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)

    if failure_point == "enqueue":
        original_enqueue = TaskQueueService.enqueue

        def flaky_enqueue(cls, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("enqueue unavailable")
            return original_enqueue(**kwargs)

        monkeypatch.setattr(
            TaskQueueService,
            "enqueue",
            classmethod(flaky_enqueue),
        )
    elif failure_point == "settle":
        original_settle = TaskQueueService.settle_bootstrap_blocker

        def flaky_settle(cls, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("settle unavailable")
            return original_settle(**kwargs)

        monkeypatch.setattr(
            TaskQueueService,
            "settle_bootstrap_blocker",
            classmethod(flaky_settle),
        )

    scheduler = build_scheduler()
    _schedule_bootstrap_job(
        scheduler,
        task_key,
        job_id=f"test_transient_{failure_point}_bootstrap",
    )
    scheduler.get_job(f"test_transient_{failure_point}_bootstrap").func()

    retry_id = f"test_transient_{failure_point}_bootstrap_completion_guard"
    retry = scheduler.get_job(retry_id)
    assert retry is not None
    retry.func(*retry.args, **retry.kwargs)

    runs = list(
        BackgroundTaskRun.select().where(BackgroundTaskRun.task_key == task_key)
    )
    assert len(runs) == 1
    assert runs[0].state == "pending"
    TaskWorker()._execute(TaskQueueService.claim_next())
    completion_guard = scheduler.get_job(retry_id)
    completion_guard.func(*completion_guard.args, **completion_guard.kwargs)

    stored = BackgroundTaskRun.get_by_id(runs[0].id)
    assert calls == ["handler"]
    assert stored.state == "completed"
    assert stored.mutex_key is None
    assert scheduler.get_job(retry_id) is None


def test_schedule_bootstrap_job_rejects_non_bootstrap_task_key():
    scheduler = build_scheduler()

    with pytest.raises(ValueError, match="unsupported_bootstrap_task_key"):
        _schedule_bootstrap_job(
            scheduler,
            "movie_heat_update",
            job_id="invalid_bootstrap",
        )


# ---------------------------------------------------------------------------
# run_job 只入队测试
# ---------------------------------------------------------------------------


def test_run_job_manual_returns_pending_task_run(test_db):
    task_run = run_job(JOB_REGISTRY_BY_KEY["movie_heat_update"], trigger_type="manual")

    assert task_run.state == "pending"
    assert task_run.task_key == "movie_heat_update"
    assert task_run.scheduled_at is not None


def test_run_job_scheduled_delegates_to_enqueue(monkeypatch):
    captured = []
    marker = object()
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.aps.enqueue_scheduled_job",
        lambda job_def: captured.append(job_def.task_key) or marker,
    )

    result = run_job(JOB_REGISTRY_BY_KEY["actor_subscription_sync"])

    assert result is marker
    assert captured == ["actor_subscription_sync"]


# ---------------------------------------------------------------------------
# get_task_logger 测试（不变）
# ---------------------------------------------------------------------------


def test_get_task_logger_reuses_same_sink_for_same_task(monkeypatch, tmp_path):
    _TASK_SINKS.clear()
    _TASK_LEVELS.clear()
    monkeypatch.setattr("src.scheduler.logging.settings.scheduler.log_dir", str(tmp_path))
    monkeypatch.setattr("src.scheduler.logging.settings.logging.level", "WARNING")

    added_sinks = []

    def fake_add(*args, **kwargs):
        added_sinks.append(kwargs["level"])
        return len(added_sinks)

    monkeypatch.setattr("src.scheduler.logging.logger.add", fake_add)

    get_task_logger("actor-subscription-sync")
    get_task_logger("actor-subscription-sync")

    assert len(_TASK_SINKS) == 1
    assert added_sinks == ["WARNING"]


def test_get_task_logger_recreates_sink_when_level_changes(monkeypatch, tmp_path):
    _TASK_SINKS.clear()
    _TASK_LEVELS.clear()
    monkeypatch.setattr("src.scheduler.logging.settings.scheduler.log_dir", str(tmp_path))

    events = {"add": [], "remove": []}

    def fake_add(*args, **kwargs):
        sink_id = len(events["add"]) + 1
        events["add"].append((sink_id, kwargs["level"]))
        return sink_id

    monkeypatch.setattr("src.scheduler.logging.logger.add", fake_add)
    monkeypatch.setattr(
        "src.scheduler.logging.logger.remove",
        lambda sink_id: events["remove"].append(sink_id),
    )

    monkeypatch.setattr("src.scheduler.logging.settings.logging.level", "INFO")
    get_task_logger("actor-subscription-sync")

    monkeypatch.setattr("src.scheduler.logging.settings.logging.level", "ERROR")
    get_task_logger("actor-subscription-sync")

    assert events["add"] == [(1, "INFO"), (2, "ERROR")]
    assert events["remove"] == [1]


# ---------------------------------------------------------------------------
# 任务架构 Wave 1：APS 只入队、worker 领取执行（docs/development/task-architecture.md）
# ---------------------------------------------------------------------------


def test_build_scheduler_wires_cron_jobs_to_enqueue_only():
    from src.start.aps import enqueue_scheduled_job

    scheduler = build_scheduler()
    jobs = scheduler.get_jobs()

    assert {job.id for job in jobs} == {job_def.task_key for job_def in JOB_REGISTRY}
    # cron 触发一律指向入队函数，绝不在 APS 线程直接执行 handler。
    assert all(job.func is enqueue_scheduled_job for job in jobs)


def test_submit_manual_job_enqueues_pending_run_without_inline_execution(test_db):
    from src.model import BackgroundTaskRun
    from src.start.aps import submit_manual_job

    job_def = JOB_REGISTRY_BY_KEY["movie_heat_update"]

    task_run = submit_manual_job(job_def)

    stored = BackgroundTaskRun.get_by_id(task_run.id)
    assert stored.state == "pending"
    assert stored.trigger_type == "manual"
    # scheduled_at 非空 = 队列托管行，等待 worker 领取，Web 进程不再起线程执行。
    assert stored.scheduled_at is not None
    assert stored.mutex_key == f"aps:{job_def.task_key}"

    with pytest.raises(TaskRunConflictError):
        submit_manual_job(job_def)


def test_task_worker_executes_claimed_queue_run(test_db, monkeypatch):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    job_def = JOB_REGISTRY_BY_KEY["movie_heat_update"]
    fake_def = job_def.model_copy(
        update={"handler": lambda _reporter, _params: calls.append(1) or {"updated_count": 1}}
    )
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, "movie_heat_update", fake_def)

    queued = TaskQueueService.enqueue(task_key="movie_heat_update", trigger_type="scheduled")
    claimed = TaskQueueService.claim_next()
    TaskWorker()._execute(claimed)

    assert calls == [1]
    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert stored.state == "completed"
    assert stored.mutex_key is None


def test_task_worker_preserves_mixed_plugin_null_vs_empty_params(test_db, monkeypatch):
    from pydantic import BaseModel

    from src.model import BackgroundTaskRun
    from src.scheduler.contracts import JobDefinition
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_mixed_worker",
        log_name="demo-mixed-worker",
        cli_name="demo-mixed-worker",
        cli_help="mixed worker",
        default_cron="0 5 * * *",
        params_schema=EmptyParams,
        handler=lambda reporter, params: calls.append(params) or {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)

    no_params = TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="scheduled",
        params=None,
    )
    TaskWorker()._execute(TaskQueueService.claim_next())
    explicit_empty = TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="manual",
        params={},
    )
    TaskWorker()._execute(TaskQueueService.claim_next())

    assert calls == [{}, {}]
    assert BackgroundTaskRun.get_by_id(no_params.id).state == "completed"
    assert BackgroundTaskRun.get_by_id(explicit_empty.id).state == "completed"


def test_task_worker_fails_run_with_unregistered_task_key(test_db):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    queued = TaskQueueService.enqueue(task_key="ghost_task", trigger_type="scheduled")
    claimed = TaskQueueService.claim_next()
    TaskWorker()._execute(claimed)

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert stored.state == "failed"
    assert stored.mutex_key is None


# ---------------------------------------------------------------------------
# 任务架构 Wave 3：并发道与队列专属任务分发
# ---------------------------------------------------------------------------


def test_default_lane_never_claims_import_lane_tasks(test_db):
    from src.scheduler.queue_tasks import NON_DEFAULT_LANE_TASK_KEYS, lane_task_keys
    from src.service.system import ActivityService
    from src.service.system.task_queue_service import TaskQueueService

    queued = ActivityService.create_task_run(
        task_key="library_import",
        trigger_type="manual",
        params={
            "media_kind": "jav",
            "library_id": 1,
            "source_ref": {"source": "test"},
        },
    )

    # default 道排除专属道任务；import 道能领到。
    assert (
        TaskQueueService.claim_next(exclude_task_keys=NON_DEFAULT_LANE_TASK_KEYS) is None
    )
    claimed = TaskQueueService.claim_next(include_task_keys=lane_task_keys("import"))
    assert claimed is not None and claimed.id == queued.id


def test_task_worker_dispatches_queue_task_handler_with_params(test_db, monkeypatch):
    from src.model import BackgroundTaskRun
    from src.scheduler.contracts import JobDefinition
    from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY
    from src.scheduler.worker import TaskWorker
    from src.service.system import ActivityService
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    monkeypatch.setitem(
        QUEUE_TASK_REGISTRY,
        "library_import",
        JobDefinition(
            task_key="library_import",
            log_name="library-import",
            cli_name="library-import",
            cli_help="library import",
            manual_only=True,
            handler=lambda reporter, params: calls.append(params) or {"ok": 1},
            lane="import",
        ),
    )
    queued = ActivityService.create_task_run(
        task_key="library_import",
        trigger_type="manual",
        params={
            "media_kind": "jav",
            "library_id": 1,
            "source_ref": {"source": "test"},
        },
    )
    claimed = TaskQueueService.claim_next()
    TaskWorker()._execute(claimed)

    assert calls == [
        {
            "media_kind": "jav",
            "library_id": 1,
            "source_ref": {"source": "test"},
        }
    ]
    assert BackgroundTaskRun.get_by_id(queued.id).state == "completed"


def test_task_worker_uses_one_job_definition_for_all_params(test_db, monkeypatch):
    """同一个 JobDefinition 统一承接无参 cron 和显式参数任务。"""
    from src.scheduler.contracts import JobDefinition
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    job_def = JobDefinition(
        task_key="demo_unified_worker",
        log_name="demo-unified-worker",
        cli_name="demo-unified-worker",
        cli_help="unified worker",
        default_cron="0 5 * * *",
        handler=lambda _reporter, params: calls.append(params.copy()) or {},
    )
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)

    TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="manual",
        params={"movie_number": "ABC-123"},
    )
    TaskWorker()._execute(TaskQueueService.claim_next())
    TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="manual",
        params={},
    )
    TaskWorker()._execute(TaskQueueService.claim_next())
    TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="scheduled",
    )
    TaskWorker()._execute(TaskQueueService.claim_next())

    assert calls == [{"movie_number": "ABC-123"}, {}, {}]
