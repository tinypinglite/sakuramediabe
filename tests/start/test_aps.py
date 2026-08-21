from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from src.config.config import settings
from src.scheduler.logging import _TASK_LEVELS, _TASK_SINKS, get_task_logger
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.service.system import TaskRunConflictError
from src.start.aps import (
    DOWNLOAD_PROGRESS_SNAPSHOT_JOB_ID,
    INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
    _bootstrap_gfriends_filetree_refresh,
    _bootstrap_movie_similarity_index,
    _schedule_bootstrap_job,
    _sync_download_progress_snapshots,
    build_scheduler,
    enqueue_scheduled_job,
    run_job,
)
from src.start.commands import main


class _FakeReporter:
    task_run_id = 1

    def progress_callback(self, _payload):
        return None


def _mock_recover_interrupted_task_runs(monkeypatch, recovered_task_runs=None):
    captured = {}

    def fake_recover_interrupted_task_runs(**kwargs):
        captured.update(kwargs)
        return list(recovered_task_runs or [])

    monkeypatch.setattr(
        "src.start.aps.ActivityService.recover_interrupted_task_runs",
        fake_recover_interrupted_task_runs,
    )
    return captured


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
# CLI 命令测试: 统一 mock run_job
# ---------------------------------------------------------------------------


def _test_cli_command(monkeypatch, cli_name, return_stats, expected_output):
    """通用 CLI 命令测试辅助函数。"""
    called = {"job": 0}

    def fake_run_job(
        job_def, *, trigger_type="scheduled", extra_callbacks=None, params=None
    ):
        called["job"] += 1
        assert trigger_type == "manual"
        return return_stats

    monkeypatch.setattr("src.start.aps.run_job", fake_run_job)

    runner = CliRunner()
    result = runner.invoke(main, ["aps", cli_name])

    assert result.exit_code == 0, result.output
    assert called["job"] == 1
    assert expected_output in result.output


def test_dynamic_aps_cli_mixed_job_distinguishes_omitted_and_explicit_empty_params(
    monkeypatch,
):
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
        service_factory=lambda reporter: {},
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: {},
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
    explicit_null = runner.invoke(
        group,
        [job_def.cli_name, "--params-json", "null"],
    )
    explicit_empty = runner.invoke(
        group,
        [job_def.cli_name, "--params-json", "{}"],
    )

    assert omitted.exit_code == 0, omitted.output
    assert explicit_null.exit_code == 0, explicit_null.output
    assert explicit_empty.exit_code == 0, explicit_empty.output
    assert captured == [(job_def, None), (job_def, None), (job_def, {})]
    assert job_def.cli_name in group.commands


def test_dynamic_aps_cli_handler_only_requires_params(monkeypatch):
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
        params_handler=lambda reporter, params: {},
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


def test_dynamic_aps_cli_factory_only_does_not_expose_params_option(monkeypatch):
    import click

    from src.scheduler.contracts import JobDefinition
    from src.start import commands as commands_module

    job_def = JobDefinition(
        task_key="demo_cli_factory_only",
        log_name="demo-cli-factory-only",
        cli_name="demo-cli-factory-only",
        cli_help="CLI factory only",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
    ).model_copy(update={"plugin_id": "demo_plugin"})
    group = click.Group()
    captured = []
    monkeypatch.setattr(
        commands_module,
        "_run_cli_job",
        lambda received_job, params=None: captured.append((received_job, params)),
    )
    commands_module._register_aps_command(job_def, group)

    result = CliRunner().invoke(
        group,
        [job_def.cli_name, "--params-json", "{}"],
    )

    assert result.exit_code != 0
    assert "No such option '--params-json'" in result.output
    assert captured == []
    assert job_def.cli_name in group.commands


def test_aps_sync_subscribed_actor_movies_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-subscribed-actor-movies",
        {"total_actors": 3, "success_actors": 2, "failed_actors": 1, "imported_movies": 5},
        "sync finished: total_actors=3 success_actors=2 failed_actors=1 imported_movies=5",
    )


def test_aps_update_movie_heat_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "update-movie-heat",
        {"candidate_count": 4, "updated_count": 3, "formula_version": "v3"},
        "heat update finished: candidate_count=4 updated_count=3 formula_version=v3",
    )


def test_aps_sync_movie_interactions_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-movie-interactions",
        {
            "candidate_movies": 4,
            "processed_movies": 4,
            "succeeded_movies": 3,
            "failed_movies": 1,
            "updated_movies": 2,
            "unchanged_movies": 1,
            "heat_updated_movies": 2,
        },
        "movie interaction sync finished: candidate_movies=4 processed_movies=4 "
        "succeeded_movies=3 failed_movies=1 updated_movies=2 unchanged_movies=1 heat_updated_movies=2",
    )


def test_aps_subcommand_prepares_database_before_running_job(monkeypatch):
    events = []

    def fake_prepare_database():
        events.append("db.ready")

    def fake_run_job(
        job_def, *, trigger_type="scheduled", extra_callbacks=None, params=None
    ):
        events.append(("job", trigger_type))
        return {"candidate_count": 1, "updated_count": 1, "formula_version": "v3"}

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


def test_aps_sync_hot_reviews_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-hot-reviews",
        {
            "total_periods": 5, "success_periods": 4, "failed_periods": 1,
            "fetched_reviews": 120, "imported_movies": 100, "skipped_reviews": 20, "stored_items": 100,
        },
        "hot review sync finished: total_periods=5 success_periods=4 failed_periods=1 "
        "fetched_reviews=120 imported_movies=100 skipped_reviews=20 stored_items=100",
    )


def test_aps_sync_movie_collections_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-movie-collections",
        {
            "total_movies": 4, "matched_count": 2, "updated_to_collection_count": 1,
            "updated_to_single_count": 1, "unchanged_count": 2,
        },
        "collection sync finished: total_movies=4 matched_count=2 "
        "updated_to_collection_count=1 updated_to_single_count=1 unchanged_count=2",
    )


def test_aps_generate_media_thumbnails_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "generate-media-thumbnails",
        {
            "pending_media": 3, "successful_media": 2, "generated_thumbnails": 6,
            "deferred_media": 0, "retryable_failed_media": 1, "terminal_failed_media": 0,
            "backend_failed_lanes": 0,
        },
        "thumbnail generation finished: pending_media=3 successful_media=2 "
        "generated_thumbnails=6 deferred_media=0 retryable_failed_media=1 terminal_failed_media=0 "
        "backend_failed_lanes=0",
    )


def test_aps_scan_media_files_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "scan-media-files",
        {
            "scanned_media": 6, "updated_media": 3, "skipped_media": 2,
            "failed_media": 1, "invalidated_media": 1, "revived_media": 1,
            "cloud115_index_failed_libraries": 1,
        },
        "media file scan finished: scanned_media=6 updated_media=3 skipped_media=2 "
        "failed_media=1 invalidated_media=1 revived_media=1 "
        "cloud115_index_failed_libraries=1",
    )


def test_aps_cleanup_download_small_files_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "cleanup-download-small-files",
        {
            "total_clients": 2, "scanned_torrents": 5, "deselected_files": 4,
            "deleted_files": 3, "failed_count": 1,
        },
        "download small file cleanup finished: total_clients=2 scanned_torrents=5 "
        "deselected_files=4 deleted_files=3 failed_count=1",
    )


def test_aps_cleanup_qb_stalled_tasks_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "cleanup-qb-stalled-tasks",
        {
            "total_clients": 2, "scanned_torrents": 5, "cleaned_count": 3,
            "failed_count": 1,
        },
        "qb stalled cleanup finished: total_clients=2 scanned_torrents=5 "
        "cleaned_count=3 failed_count=1",
    )


def test_aps_sync_cloud115_offline_tasks_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-cloud115-offline-tasks",
        {
            "total_clients": 1, "updated_count": 3, "import_triggered_count": 1,
            "abandoned_count": 1, "failed_count": 0,
        },
        "cloud115 offline sync finished: total_clients=1 updated_count=3 "
        "import_triggered_count=1 abandoned_count=1 failed_count=0",
    )


def test_aps_cleanup_activity_records_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "cleanup-activity-records",
        {"deleted_task_runs": 30, "deleted_notifications": 5},
        "activity record cleanup finished: deleted_task_runs=30 deleted_notifications=5",
    )


def test_aps_index_image_search_thumbnails_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "index-image-search-thumbnails",
        {"pending_thumbnails": 4, "successful_thumbnails": 3, "failed_thumbnails": 1},
        "image search index finished: pending_thumbnails=4 successful_thumbnails=3 failed_thumbnails=1",
    )


def test_aps_optimize_image_search_index_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "optimize-image-search-index",
        {"optimized": True},
        "image search optimize finished: optimized=True",
    )


def test_aps_recompute_movie_similarities_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "recompute-movie-similarities",
        {
            "total_movies": 8,
            "indexed_movies": 7,
            "actor_features": 18,
            "tag_features": 24,
        },
        "movie similarity recompute finished: total_movies=8 indexed_movies=7 "
        "actor_features=18 tag_features=24",
    )


def test_aps_generate_daily_recommendations_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "generate-daily-recommendations",
        {
            "candidate_movies": 8,
            "stored_items": 5,
            "cold_start": True,
            "extreme_cold_start": False,
        },
        "daily recommendation generate finished: candidate_movies=8 stored_items=5 "
        "cold_start=True extreme_cold_start=False",
    )


def test_aps_generate_moment_recommendations_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "generate-moment-recommendations",
        {
            "seed_points": 3,
            "visual_candidates": 4,
            "similar_candidates": 2,
            "popular_candidates": 1,
            "stored_items": 5,
        },
        "moment recommendation generate finished: seed_points=3 visual_candidates=4 "
        "similar_candidates=2 popular_candidates=1 stored_items=5",
    )


def test_aps_auto_download_subscribed_movies_command_invokes_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "auto-download-subscribed-movies",
        {
            "candidate_movies": 3, "searched_movies": 3, "submitted_movies": 2,
            "no_candidate_movies": 1, "skipped_movies": 0, "failed_movies": 0,
        },
        "auto download finished: candidate_movies=3 searched_movies=3 submitted_movies=2 "
        "no_candidate_movies=1 skipped_movies=0 failed_movies=0",
    )


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
    monkeypatch.setattr("src.start.aps.settings.scheduler.download_small_file_cleanup_cron", "*/5 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_collection_sync_cron", "0 1 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_file_scan_cron", "0 */6 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_interaction_sync_cron", "0 5 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_thumbnail_cron", "*/5 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_index_cron", "*/10 * * * *")
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
    assert str(scheduler.get_job("movie_collection_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='0']"
    assert str(scheduler.get_job("hot_review_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='20']"
    assert str(scheduler.get_job("movie_interaction_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='5', minute='0']"
    assert str(scheduler.get_job("movie_heat_update").trigger) == "cron[month='*', day='*', day_of_week='*', hour='0', minute='15']"
    assert str(scheduler.get_job("download_task_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/15']"
    assert str(scheduler.get_job("download_task_auto_import").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(scheduler.get_job("download_small_file_cleanup").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/5']"
    assert str(scheduler.get_job("media_file_scan").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*/6', minute='0']"
    assert str(scheduler.get_job("media_thumbnail_generation").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/5']"
    assert str(scheduler.get_job("image_search_index").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
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


def test_similarity_bootstrap_enqueues_startup_run_and_worker_executes_factory(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    job_def = JOB_REGISTRY_BY_KEY["movie_similarity_recompute"]
    fake_def = job_def.model_copy(
        update={"service_factory": lambda _reporter: calls.append("factory") or {}}
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
    assert calls == ["factory"]
    assert stored.state == "completed"
    assert stored.mutex_key is None


def test_gfriends_bootstrap_explicit_params_preserve_force_false_and_cron_null_factory(
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
    # cron factory 在 registry 模块绑定同一函数名，单独替换以验证 NULL 分派。
    monkeypatch.setattr(
        "src.scheduler.registry.refresh_gfriends_filetree",
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
            update={"service_factory": lambda _reporter: calls.append("factory") or {}}
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

    assert calls == ["factory"]
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
            update={"service_factory": lambda _reporter: calls.append("factory") or {}}
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
    assert calls == ["factory"]
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
            update={"service_factory": lambda _reporter: calls.append("factory") or {}}
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
    assert calls == ["factory"]
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
            update={"service_factory": lambda _reporter: calls.append("factory") or {}}
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
    assert calls == ["factory"]
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
# 启动恢复测试
# ---------------------------------------------------------------------------


def test_aps_recovers_interrupted_scheduled_tasks_before_starting_scheduler(monkeypatch):
    events = []

    class FakeScheduler:
        def start(self):
            events.append("scheduler.start")

    monkeypatch.setattr("src.start.aps.settings.scheduler.enabled", True)
    fake_database = object()
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: events.append("db.ready") or fake_database)
    def fake_recover_interrupted_task_runs(**kwargs):
        events.append(("recover", kwargs))
        return []

    monkeypatch.setattr("src.start.recovery.ActivityService.recover_interrupted_task_runs", fake_recover_interrupted_task_runs)
    monkeypatch.setattr("src.start.aps.build_scheduler", lambda: events.append("build") or FakeScheduler())
    monkeypatch.setattr("src.start.aps._bootstrap_movie_similarity_index", lambda _scheduler: None)

    from src.start.aps import aps

    aps()

    assert events == [
        "db.ready",
        (
            "recover",
            {
                "trigger_type": "scheduled",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
                "suppress_notification_task_keys": {"media_rapid_upload"},
            },
        ),
        (
            "recover",
            {
                "trigger_type": "manual",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
                "suppress_notification_task_keys": {"media_rapid_upload"},
            },
        ),
        (
            "recover",
            {
                "trigger_type": "internal",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
                "suppress_notification_task_keys": {"media_rapid_upload"},
            },
        ),
        (
            "recover",
            {
                "trigger_type": "startup",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
                "suppress_notification_task_keys": {"media_rapid_upload"},
            },
        ),
        "build",
        "scheduler.start",
    ]


def test_aps_recovers_task_related_business_running_states(monkeypatch):
    events = []

    class FakeScheduler:
        def start(self):
            events.append("scheduler.start")

    monkeypatch.setattr("src.start.aps.settings.scheduler.enabled", True)
    fake_database = object()
    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: events.append("db.ready") or fake_database)

    def fake_recover_interrupted_task_runs(**kwargs):
        events.append(("recover", kwargs["trigger_type"]))
        if kwargs["trigger_type"] == "scheduled":
            return [type("TaskRun", (), {"task_key": "movie_interaction_sync"})()]
        if kwargs["trigger_type"] == "manual":
            return [type("TaskRun", (), {"task_key": "library_import"})()]
        if kwargs["trigger_type"] == "internal":
            return [type("TaskRun", (), {"task_key": "library_import"})()]
        return []

    monkeypatch.setattr("src.start.recovery.ActivityService.recover_interrupted_task_runs", fake_recover_interrupted_task_runs)
    from src.start.recovery import BUSINESS_RECOVERY_HANDLERS

    monkeypatch.setitem(
        BUSINESS_RECOVERY_HANDLERS,
        "library_import",
        lambda: events.append(("recover_import", True)) or {"recovered_count": 1},
    )
    monkeypatch.setattr("src.start.aps.build_scheduler", lambda: events.append("build") or FakeScheduler())
    monkeypatch.setattr("src.start.aps._bootstrap_movie_similarity_index", lambda _scheduler: None)

    from src.start.aps import aps

    aps()

    assert events == [
        "db.ready",
        ("recover", "scheduled"),
        ("recover", "manual"),
        ("recover", "internal"),
        ("recover", "startup"),
        ("recover_import", True),
        "build",
        "scheduler.start",
    ]


def test_library_import_recovery_resets_only_linked_download_imports(monkeypatch):
    from src.start.recovery import BUSINESS_RECOVERY_HANDLERS, recover_interrupted_tasks

    monkeypatch.setattr(
        "src.start.recovery.ActivityService.recover_interrupted_task_runs",
        lambda **kwargs: [
            type("TaskRun", (), {"task_key": "library_import"})()
        ]
        if kwargs["trigger_type"] == "manual"
        else [],
    )
    calls = []
    monkeypatch.setitem(
        BUSINESS_RECOVERY_HANDLERS,
        "library_import",
        lambda: calls.append("imports") or 1,
    )

    recovered = recover_interrupted_tasks(
        trigger_types=("manual",), error_message="容器重启"
    )

    assert recovered == {"library_import"}
    assert calls == ["imports"]


def test_subtitle_task_run_has_no_domain_recovery_handler():
    from src.start.recovery import BUSINESS_RECOVERY_HANDLERS

    assert "subtitle_directory_import" not in BUSINESS_RECOVERY_HANDLERS


# ---------------------------------------------------------------------------
# run_job 直接调用测试
# ---------------------------------------------------------------------------


def test_run_job_ensures_database_and_calls_activity_service(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_task(
        *,
        task_key,
        trigger_type,
        func,
        task_name=None,
        task_run_id=None,
        log_task_name=None,
        extra_callbacks=None,
        mutex_key=None,
        conflict_policy="raise",
    ):
        events.append(("run_task", task_key, log_task_name, mutex_key, conflict_policy))
        return func(_FakeReporter())

    monkeypatch.setattr("src.start.aps.ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.ActivityService.run_task", fake_run_task)
    recovered_payload = _mock_recover_interrupted_task_runs(monkeypatch)

    job_def = JOB_REGISTRY_BY_KEY["movie_heat_update"]
    monkeypatch.setattr(
        "src.scheduler.registry.MovieHeatService.update_movie_heat",
        lambda: {
            "candidate_count": 12, "updated_count": 11, "formula_version": "v3",
        },
    )

    result = run_job(job_def)

    assert result["candidate_count"] == 12
    assert recovered_payload == {
        "task_key": "movie_heat_update",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }
    assert events == ["ready", ("run_task", "movie_heat_update", "movie-heat-update", "aps:movie_heat_update", "skip")]


def test_run_job_mixed_explicit_empty_params_uses_params_handler(monkeypatch):
    from pydantic import BaseModel

    from src.scheduler.contracts import JobDefinition

    calls = []

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_sync_mixed",
        log_name="demo-sync-mixed",
        cli_name="demo-sync-mixed",
        cli_help="sync mixed",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: calls.append("factory") or {},
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: calls.append(("params", params)) or {},
    ).model_copy(update={"plugin_id": "demo_plugin"})

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    _mock_recover_interrupted_task_runs(monkeypatch)
    monkeypatch.setattr(
        "src.start.aps.ActivityService.run_task",
        lambda **kwargs: kwargs["func"](_FakeReporter()),
    )

    result = run_job(job_def, trigger_type="manual", params={})

    assert result == {}
    assert calls == [("params", {})]


def test_run_job_manual_uses_raise_conflict_policy(monkeypatch):
    captured = {}

    def fake_run_task(
        *,
        task_key,
        trigger_type,
        func,
        task_name=None,
        task_run_id=None,
        log_task_name=None,
        extra_callbacks=None,
        mutex_key=None,
        conflict_policy="raise",
    ):
        captured.update(
            {
                "task_key": task_key,
                "trigger_type": trigger_type,
                "mutex_key": mutex_key,
                "conflict_policy": conflict_policy,
            }
        )
        return {"ok": True}

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    monkeypatch.setattr("src.start.aps.ActivityService.run_task", fake_run_task)
    recovered_payload = _mock_recover_interrupted_task_runs(monkeypatch)

    result = run_job(JOB_REGISTRY_BY_KEY["actor_subscription_sync"], trigger_type="manual")

    assert result == {"ok": True}
    assert recovered_payload == {
        "task_key": "actor_subscription_sync",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }
    assert captured == {
        "task_key": "actor_subscription_sync",
        "trigger_type": "manual",
        "mutex_key": "aps:actor_subscription_sync",
        "conflict_policy": "raise",
    }


def test_run_job_scheduled_skip_logs_and_returns_skip_payload(monkeypatch):
    events = []

    def fake_run_task(
        *,
        task_key,
        trigger_type,
        func,
        task_name=None,
        task_run_id=None,
        log_task_name=None,
        extra_callbacks=None,
        mutex_key=None,
        conflict_policy="raise",
    ):
        return {
            "task_skipped": True,
            "reason": "mutex_conflict",
            "blocking_task_run_id": 7,
            "blocking_trigger_type": "manual",
        }

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    monkeypatch.setattr("src.start.aps.ActivityService.run_task", fake_run_task)
    monkeypatch.setattr("src.start.aps.logger.info", lambda message, *args: events.append(message.format(*args)))
    recovered_payload = _mock_recover_interrupted_task_runs(monkeypatch)

    result = run_job(JOB_REGISTRY_BY_KEY["actor_subscription_sync"], trigger_type="scheduled")

    assert result["task_skipped"] is True
    assert recovered_payload == {
        "task_key": "actor_subscription_sync",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }
    assert any("定时任务因同任务仍在运行而跳过 task_key=actor_subscription_sync" in event for event in events)


def test_run_job_recovers_task_runs_for_job_without_business_recovery(monkeypatch):
    def fake_run_task(
        *,
        task_key,
        trigger_type,
        func,
        task_name=None,
        task_run_id=None,
        log_task_name=None,
        extra_callbacks=None,
        mutex_key=None,
        conflict_policy="raise",
    ):
        return func(_FakeReporter())

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: None)
    monkeypatch.setattr("src.start.aps.ActivityService.run_task", fake_run_task)
    recovered_payload = _mock_recover_interrupted_task_runs(monkeypatch, recovered_task_runs=[object()])
    monkeypatch.setattr(
        "src.scheduler.registry.MovieHeatService.update_movie_heat",
        lambda: {"candidate_count": 1, "updated_count": 1, "formula_version": "v3"},
    )

    result = run_job(JOB_REGISTRY_BY_KEY["movie_heat_update"])

    assert result["recovered_task_runs"] == 1
    assert recovered_payload == {
        "task_key": "movie_heat_update",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }


# ---------------------------------------------------------------------------
# ActivityService.run_task 日志测试（原 run_tracked_task / run_logged_task 测试）
# ---------------------------------------------------------------------------


def test_activity_service_run_task_with_logging(monkeypatch):
    events = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

        def exception(self, message, *args):
            events.append(("exception", message.format(*args) if args else message))

    monkeypatch.setattr("src.scheduler.logging.get_task_logger", lambda task_name: FakeLogger())

    class FakeTaskRun:
        id = 1
        task_key = "actor_subscription_sync"
        trigger_type = "scheduled"
        state = "running"
        result_summary = {}

    monkeypatch.setattr(
        "src.service.system.activity_service.ActivityService.create_task_run",
        staticmethod(lambda **kwargs: FakeTaskRun()),
    )
    monkeypatch.setattr(
        "src.service.system.activity_service.ActivityService.mark_task_run_running",
        staticmethod(lambda task_run_id: FakeTaskRun()),
    )
    monkeypatch.setattr(
        "src.service.system.activity_service.ActivityService.complete_task_run",
        classmethod(lambda cls, task_run_id, **kwargs: FakeTaskRun()),
    )
    monkeypatch.setattr(
        "src.service.system.activity_service.ActivityService._complete_task_run_transition",
        classmethod(lambda cls, task_run_id, **kwargs: (FakeTaskRun(), True)),
    )
    monkeypatch.setattr(
        "src.service.system.activity_service.ActivityService.update_task_run_progress",
        staticmethod(lambda task_run_id, **kwargs: FakeTaskRun()),
    )

    from src.service.system.activity_service import ActivityService

    result = ActivityService.run_task(
        task_key="actor_subscription_sync",
        trigger_type="scheduled",
        func=lambda reporter: {"ok": True},
        log_task_name="actor-subscription-sync",
    )

    assert result == {"ok": True}
    assert events[0][0] == "info"
    assert events[-1][0] == "info"


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

    assert {job.id for job in jobs} == {
        *(job_def.task_key for job_def in JOB_REGISTRY),
        DOWNLOAD_PROGRESS_SNAPSHOT_JOB_ID,
    }
    # cron 触发一律指向入队函数，绝不直接执行 service_factory。
    cron_jobs = [job for job in jobs if job.id != DOWNLOAD_PROGRESS_SNAPSHOT_JOB_ID]
    assert all(job.func is enqueue_scheduled_job for job in cron_jobs)


def test_build_scheduler_wires_internal_download_progress_sampler_without_task_key():
    from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY

    scheduler = build_scheduler()
    job = scheduler.get_job(DOWNLOAD_PROGRESS_SNAPSHOT_JOB_ID)

    assert job is not None
    assert job.func is _sync_download_progress_snapshots
    assert job.coalesce is True
    assert job.max_instances == 1
    assert job.id not in JOB_REGISTRY_BY_KEY
    assert job.id not in QUEUE_TASK_REGISTRY
    assert job.trigger.interval.total_seconds() == pytest.approx(
        settings.scheduler.download_progress_snapshot_interval_seconds
    )


def test_internal_download_progress_sampler_writes_directly_without_task_run(monkeypatch):
    events = []

    class FakeProgressSyncService:
        def sync_all_clients(self):
            events.append("sync")

    monkeypatch.setattr("src.start.aps.ensure_database_ready", lambda: events.append("db"))
    monkeypatch.setattr(
        "src.start.aps.DownloadProgressSyncService",
        FakeProgressSyncService,
    )
    monkeypatch.setattr(
        "src.start.aps.ActivityService.run_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not create TaskRun")),
    )

    _sync_download_progress_snapshots()

    assert events == ["db", "sync"]


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
        update={"service_factory": lambda _reporter: calls.append(1) or {"updated_count": 1}}
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
        service_factory=lambda reporter: calls.append("factory") or {},
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: calls.append(("params", params)) or {},
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

    assert calls == ["factory", ("params", {})]
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


def test_task_worker_resolution_failure_releases_mutex(test_db, monkeypatch):
    from src.model import BackgroundTaskRun
    from src.scheduler.contracts import JobDefinition
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    job_def = JobDefinition(
        task_key="factory_only_invalid_params",
        log_name="factory-only-invalid-params",
        cli_name="factory-only-invalid-params",
        cli_help="factory only invalid params",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
    )
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, job_def.task_key, job_def)
    queued = TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="manual",
        params={"unexpected": True},
    )

    TaskWorker()._execute(TaskQueueService.claim_next())

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert stored.state == "failed"
    assert stored.error_message == (f"任务不支持带参执行 task_key={job_def.task_key}")
    assert stored.mutex_key is None
    assert (
        TaskQueueService.enqueue(
            task_key=job_def.task_key,
            trigger_type="scheduled",
        )
        is not None
    )


def test_task_worker_rapid_failure_suppresses_generic_notification(
    test_db, monkeypatch
):
    from src.model import BackgroundTaskRun, SystemNotification
    from src.scheduler.queue_tasks import (
        LANE_RAPID_UPLOAD,
        QUEUE_TASK_REGISTRY,
        QueueTaskDefinition,
    )
    from src.scheduler.worker import TaskWorker
    from src.service.system.task_queue_service import TaskQueueService

    def fail_rapid_upload(_reporter, _params):
        raise RuntimeError("rapid failed")

    monkeypatch.setitem(
        QUEUE_TASK_REGISTRY,
        "media_rapid_upload",
        QueueTaskDefinition(
            task_key="media_rapid_upload",
            log_name="media-rapid-upload",
            handler=fail_rapid_upload,
            lane=LANE_RAPID_UPLOAD,
            notify_result=False,
        ),
    )
    queued = TaskQueueService.enqueue(
        task_key="media_rapid_upload",
        trigger_type="manual",
        params={"rapid_upload_batch_id": 7},
    )

    TaskWorker()._execute(TaskQueueService.claim_next())

    stored = BackgroundTaskRun.get_by_id(queued.id)
    assert stored.state == "failed"
    assert stored.error_message == "rapid failed"
    assert SystemNotification.select().count() == 0


# ---------------------------------------------------------------------------
# 任务架构 Wave 3：并发道与队列专属任务分发
# ---------------------------------------------------------------------------


def test_default_lane_never_claims_import_lane_tasks(test_db):
    from src.common.runtime_time import utc_now_for_db
    from src.scheduler.queue_tasks import NON_DEFAULT_LANE_TASK_KEYS, lane_task_keys
    from src.service.system.activity_service import ActivityService
    from src.service.system.task_queue_service import TaskQueueService

    queued = ActivityService.create_task_run(
        task_key="library_import",
        trigger_type="manual",
        params={"media_kind": "jav", "backend": "local"},
        scheduled_at=utc_now_for_db(),
    )

    # default 道排除专属道任务；import 道能领到。
    assert (
        TaskQueueService.claim_next(exclude_task_keys=NON_DEFAULT_LANE_TASK_KEYS) is None
    )
    claimed = TaskQueueService.claim_next(include_task_keys=lane_task_keys("import"))
    assert claimed is not None and claimed.id == queued.id


def test_task_worker_dispatches_queue_task_handler_with_params(test_db, monkeypatch):
    from src.common.runtime_time import utc_now_for_db
    from src.model import BackgroundTaskRun
    from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY, QueueTaskDefinition
    from src.scheduler.worker import TaskWorker
    from src.service.system.activity_service import ActivityService
    from src.service.system.task_queue_service import TaskQueueService

    calls = []
    monkeypatch.setitem(
        QUEUE_TASK_REGISTRY,
        "library_import",
        QueueTaskDefinition(
            task_key="library_import",
            log_name="library-import",
            handler=lambda reporter, params: calls.append(params) or {"ok": 1},
            lane="import",
        ),
    )
    queued = ActivityService.create_task_run(
        task_key="library_import",
        trigger_type="manual",
        params={"media_kind": "jav", "backend": "local"},
        scheduled_at=utc_now_for_db(),
    )
    claimed = TaskQueueService.claim_next()
    TaskWorker()._execute(claimed)

    assert calls == [{"media_kind": "jav", "backend": "local"}]
    assert BackgroundTaskRun.get_by_id(queued.id).state == "completed"


def test_task_worker_single_movie_params_beats_cron_factory(test_db, monkeypatch):
    """与 cron 同 key 的运行带 params 时走单资源 handler，不带 params 走批任务 factory。"""
    from src.common.runtime_time import utc_now_for_db
    from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY, QueueTaskDefinition
    from src.scheduler.worker import TaskWorker
    from src.service.system.activity_service import ActivityService
    from src.service.system.task_queue_service import TaskQueueService

    single_calls = []
    batch_calls = []
    monkeypatch.setitem(
        QUEUE_TASK_REGISTRY,
        "movie_interaction_sync",
        QueueTaskDefinition(
            task_key="movie_interaction_sync",
            log_name="movie-interaction-sync",
            handler=lambda reporter, params: single_calls.append(params) or {},
        ),
    )
    job_def = JOB_REGISTRY_BY_KEY["movie_interaction_sync"]
    fake_def = job_def.model_copy(
        update={"service_factory": lambda _reporter: batch_calls.append(1) or {}}
    )
    monkeypatch.setitem(JOB_REGISTRY_BY_KEY, "movie_interaction_sync", fake_def)

    ActivityService.create_task_run(
        task_key="movie_interaction_sync",
        trigger_type="manual",
        params={"movie_id": 42},
        scheduled_at=utc_now_for_db(),
    )
    TaskWorker()._execute(TaskQueueService.claim_next())
    ActivityService.create_task_run(
        task_key="movie_interaction_sync",
        trigger_type="manual",
        params={},
        scheduled_at=utc_now_for_db(),
    )
    TaskWorker()._execute(TaskQueueService.claim_next())
    ActivityService.create_task_run(
        task_key="movie_interaction_sync",
        trigger_type="scheduled",
        scheduled_at=utc_now_for_db(),
    )
    TaskWorker()._execute(TaskQueueService.claim_next())

    assert single_calls == [{"movie_id": 42}, {}]
    assert batch_calls == [1]
