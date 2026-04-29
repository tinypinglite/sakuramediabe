import pytest
from click.testing import CliRunner
from loguru import logger
from zoneinfo import ZoneInfo

from src.scheduler.logging import _TASK_LEVELS, _TASK_SINKS, get_task_logger
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.service.system import TaskRunConflictError
from src.start.aps import INTERRUPTED_TASK_RUN_ERROR_MESSAGE, build_scheduler, run_job
from src.start.commands import main


class _FakeReporter:
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

    def fake_run_job(job_def, *, trigger_type="scheduled", extra_callbacks=None):
        called["job"] += 1
        assert trigger_type == "manual"
        return return_stats

    monkeypatch.setattr("src.start.aps.run_job", fake_run_job)

    runner = CliRunner()
    result = runner.invoke(main, ["aps", cli_name])

    assert result.exit_code == 0, result.output
    assert called["job"] == 1
    assert expected_output in result.output


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
        {"candidate_count": 4, "updated_count": 3, "formula_version": "v1"},
        "heat update finished: candidate_count=4 updated_count=3 formula_version=v1",
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

    def fake_run_job(job_def, *, trigger_type="scheduled", extra_callbacks=None):
        events.append(("job", trigger_type))
        return {"candidate_count": 1, "updated_count": 1, "formula_version": "v1"}

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


def test_aps_sync_rankings_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-rankings",
        {
            "total_targets": 12, "success_targets": 11, "failed_targets": 1,
            "fetched_numbers": 240, "imported_movies": 220, "skipped_movies": 20, "stored_items": 220,
        },
        "ranking sync finished: total_targets=12 success_targets=11 failed_targets=1 "
        "fetched_numbers=240 imported_movies=220 skipped_movies=20 stored_items=220",
    )


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
            "retryable_failed_media": 1, "terminal_failed_media": 0,
        },
        "thumbnail generation finished: pending_media=3 successful_media=2 "
        "generated_thumbnails=6 retryable_failed_media=1 terminal_failed_media=0",
    )


def test_aps_scan_media_files_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "scan-media-files",
        {
            "scanned_media": 6, "updated_media": 3, "skipped_media": 2,
            "failed_media": 1, "invalidated_media": 1, "revived_media": 1,
        },
        "media file scan finished: scanned_media=6 updated_media=3 skipped_media=2 "
        "failed_media=1 invalidated_media=1 revived_media=1",
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
        {"compacted": True},
        "image search optimize finished: compacted=True",
    )


def test_aps_recompute_movie_similarities_command_runs_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "recompute-movie-similarities",
        {
            "total_movies": 8,
            "processed_movies": 8,
            "stored_pairs": 42,
            "skipped_movies": 1,
        },
        "movie similarity recompute finished: total_movies=8 processed_movies=8 "
        "stored_pairs=42 skipped_movies=1",
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


def test_aps_sync_movie_desc_command_invokes_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "sync-movie-desc",
        {
            "candidate_movies": 3,
            "processed_movies": 3,
            "succeeded_movies": 2,
            "failed_movies": 1,
            "updated_movies": 2,
            "skipped_movies": 0,
        },
        "movie desc sync finished: candidate_movies=3 processed_movies=3 "
        "succeeded_movies=2 failed_movies=1 updated_movies=2 skipped_movies=0",
    )


def test_aps_translate_movie_desc_command_invokes_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "translate-movie-desc",
        {
            "candidate_movies": 3,
            "processed_movies": 3,
            "succeeded_movies": 2,
            "failed_movies": 1,
            "updated_movies": 2,
            "skipped_movies": 0,
        },
        "movie desc translation finished: candidate_movies=3 processed_movies=3 "
        "succeeded_movies=2 failed_movies=1 updated_movies=2 skipped_movies=0",
    )


def test_aps_translate_movie_title_command_invokes_job(monkeypatch):
    _test_cli_command(
        monkeypatch,
        "translate-movie-title",
        {
            "candidate_movies": 3,
            "processed_movies": 3,
            "succeeded_movies": 2,
            "failed_movies": 1,
            "updated_movies": 2,
            "skipped_movies": 0,
        },
        "movie title translation finished: candidate_movies=3 processed_movies=3 "
        "succeeded_movies=2 failed_movies=1 updated_movies=2 skipped_movies=0",
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
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_collection_sync_cron", "0 1 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_file_scan_cron", "0 */6 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_desc_sync_cron", "0 4 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_interaction_sync_cron", "0 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_desc_translation_cron", "15 4 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_title_translation_cron", "20 4 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_thumbnail_cron", "*/5 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_index_cron", "*/10 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_optimize_cron", "0 */6 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_similarity_recompute_cron", "30 3 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.ranking_sync_cron", "10 1 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.hot_review_sync_cron", "20 1 * * *")

    scheduler = build_scheduler()

    # 验证所有任务都已注册
    for job_def in JOB_REGISTRY:
        job = scheduler.get_job(job_def.task_key)
        assert job is not None, f"Job {job_def.task_key} not registered"

    # 验证部分 cron 表达式
    assert str(scheduler.get_job("actor_subscription_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='2', minute='0']"
    assert str(scheduler.get_job("subscribed_movie_auto_download").trigger) == "cron[month='*', day='*', day_of_week='*', hour='2', minute='30']"
    assert str(scheduler.get_job("movie_collection_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='0']"
    assert str(scheduler.get_job("ranking_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='10']"
    assert str(scheduler.get_job("hot_review_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='20']"
    assert str(scheduler.get_job("movie_desc_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='4', minute='0']"
    assert str(scheduler.get_job("movie_interaction_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='0']"
    assert str(scheduler.get_job("movie_desc_translation").trigger) == "cron[month='*', day='*', day_of_week='*', hour='4', minute='15']"
    assert str(scheduler.get_job("movie_title_translation").trigger) == "cron[month='*', day='*', day_of_week='*', hour='4', minute='20']"
    assert str(scheduler.get_job("movie_heat_update").trigger) == "cron[month='*', day='*', day_of_week='*', hour='0', minute='15']"
    assert str(scheduler.get_job("download_task_sync").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/15']"
    assert str(scheduler.get_job("download_task_auto_import").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(scheduler.get_job("media_file_scan").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*/6', minute='0']"
    assert str(scheduler.get_job("media_thumbnail_generation").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/5']"
    assert str(scheduler.get_job("image_search_index").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(scheduler.get_job("image_search_optimize").trigger) == "cron[month='*', day='*', day_of_week='*', hour='*/6', minute='0']"
    assert str(scheduler.get_job("movie_similarity_recompute").trigger) == "cron[month='*', day='*', day_of_week='*', hour='3', minute='30']"
    assert scheduler.timezone.key == "Asia/Shanghai"


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
    monkeypatch.setattr(
        "src.start.recovery.DownloadSyncService.recover_orphaned_imports_only",
        lambda self: (_ for _ in ()).throw(AssertionError("should not recover import state")),
    )
    monkeypatch.setattr("src.start.aps.build_scheduler", lambda: events.append("build") or FakeScheduler())

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
            },
        ),
        (
            "recover",
            {
                "trigger_type": "manual",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
            },
        ),
        (
            "recover",
            {
                "trigger_type": "internal",
                "error_message": "APS进程重启，任务已中断",
                "allow_null_owner": True,
                "force": True,
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
            return [type("TaskRun", (), {"task_key": "movie_desc_sync"})()]
        if kwargs["trigger_type"] == "manual":
            return [type("TaskRun", (), {"task_key": "movie_desc_translation"})()]
        if kwargs["trigger_type"] == "internal":
            return [type("TaskRun", (), {"task_key": "download_task_import"})()]
        return []

    monkeypatch.setattr("src.start.recovery.ActivityService.recover_interrupted_task_runs", fake_recover_interrupted_task_runs)
    monkeypatch.setattr(
        "src.start.recovery.MovieDescSyncService.recover_interrupted_running_movies",
        lambda **kwargs: events.append(("recover_desc", kwargs["error_message"])) or 1,
    )
    monkeypatch.setattr(
        "src.start.recovery.MovieDescTranslationService.recover_interrupted_running_movies",
        lambda **kwargs: events.append(("recover_translation", kwargs["error_message"])) or 2,
    )
    monkeypatch.setattr(
        "src.start.recovery.DownloadSyncService.recover_orphaned_imports_only",
        lambda self: events.append(("recover_import", True)) or {"recovered_count": 1},
    )
    monkeypatch.setattr("src.start.aps.build_scheduler", lambda: events.append("build") or FakeScheduler())

    from src.start.aps import aps

    aps()

    assert events == [
        "db.ready",
        ("recover", "scheduled"),
        ("recover", "manual"),
        ("recover", "internal"),
        ("recover_desc", "影片描述抓取任务中断，等待重试"),
        ("recover_translation", "影片简介翻译任务中断，等待重试"),
        ("recover_import", True),
        "build",
        "scheduler.start",
    ]


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

    job_def = JOB_REGISTRY_BY_KEY["ranking_sync"]
    monkeypatch.setattr(
        "src.scheduler.registry.RankingSyncService.sync_all_rankings",
        lambda self, progress_callback=None: {
            "total_targets": 12, "success_targets": 12, "failed_targets": 0,
            "fetched_numbers": 240, "imported_movies": 230, "skipped_movies": 10, "stored_items": 230,
        },
    )

    result = run_job(job_def)

    assert result["total_targets"] == 12
    assert recovered_payload == {
        "task_key": "ranking_sync",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }
    assert events == ["ready", ("run_task", "ranking_sync", "ranking-sync", "aps:ranking_sync", "skip")]


def test_run_job_movie_desc_sync_with_recovery(monkeypatch):
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
        events.append(("run_task", task_key))
        return func(_FakeReporter())

    monkeypatch.setattr("src.start.aps.ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.ActivityService.run_task", fake_run_task)
    recovered_payload = _mock_recover_interrupted_task_runs(monkeypatch, recovered_task_runs=[object()])

    class FakeMovieDescSyncService:
        INTERRUPTED_FETCH_ERROR_MESSAGE = "影片描述抓取任务中断，等待重试"

        @classmethod
        def recover_interrupted_running_movies(cls, **kwargs):
            return 2

        def run(self, progress_callback=None):
            return {
                "candidate_movies": 2,
                "processed_movies": 2,
                "succeeded_movies": 1,
                "failed_movies": 1,
                "updated_movies": 1,
                "skipped_movies": 0,
            }

    monkeypatch.setattr("src.scheduler.registry.MovieDescSyncService", FakeMovieDescSyncService)

    job_def = JOB_REGISTRY_BY_KEY["movie_desc_sync"]
    result = run_job(job_def)

    assert result["processed_movies"] == 2
    assert result["recovered_task_runs"] == 1
    assert result["recovered_running_movies"] == 2
    assert recovered_payload == {
        "task_key": "movie_desc_sync",
        "error_message": INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        "allow_null_owner": True,
    }


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
        "src.scheduler.registry.RankingSyncService.sync_all_rankings",
        lambda self, progress_callback=None: {
            "total_targets": 1,
            "success_targets": 1,
            "failed_targets": 0,
            "fetched_numbers": 10,
            "imported_movies": 10,
            "skipped_movies": 0,
            "stored_items": 10,
        },
    )

    result = run_job(JOB_REGISTRY_BY_KEY["ranking_sync"])

    assert result["recovered_task_runs"] == 1
    assert recovered_payload == {
        "task_key": "ranking_sync",
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
