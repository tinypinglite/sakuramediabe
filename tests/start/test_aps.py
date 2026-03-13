import pytest
from click.testing import CliRunner
from loguru import logger

from src.scheduler.logging import _TASK_LEVELS, _TASK_SINKS, get_task_logger
from src.scheduler.runner import run_logged_task
from src.start.aps import build_scheduler
from src.start.commands import main


def test_aps_command_invokes_scheduler_entrypoint(monkeypatch):
    called = {"aps": 0}

    def fake_aps():
        called["aps"] += 1

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.aps", fake_aps)

    result = runner.invoke(main, ["aps"])

    assert result.exit_code == 0
    assert called["aps"] == 1


def test_aps_sync_subscribed_actor_movies_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {
            "total_actors": 3,
            "success_actors": 2,
            "failed_actors": 1,
            "imported_movies": 5,
        }

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_subscribed_actor_movie_sync_job", fake_run_job)

    result = runner.invoke(main, ["aps", "sync-subscribed-actor-movies"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert (
        "sync finished: total_actors=3 success_actors=2 failed_actors=1 imported_movies=5"
        in result.output
    )


def test_aps_update_movie_heat_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {
            "candidate_count": 4,
            "updated_count": 3,
            "formula_version": "v1",
        }

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_movie_heat_update_job", fake_run_job)

    result = runner.invoke(main, ["aps", "update-movie-heat"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert (
        "heat update finished: candidate_count=4 updated_count=3 formula_version=v1"
        in result.output
    )


def test_aps_sync_movie_collections_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {
            "total_movies": 4,
            "matched_count": 2,
            "updated_to_collection_count": 1,
            "updated_to_single_count": 1,
            "unchanged_count": 2,
        }

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_movie_collection_sync_job", fake_run_job)

    result = runner.invoke(main, ["aps", "sync-movie-collections"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert (
        "collection sync finished: total_movies=4 matched_count=2 "
        "updated_to_collection_count=1 updated_to_single_count=1 unchanged_count=2"
        in result.output
    )


def test_aps_generate_media_thumbnails_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {
            "pending_media": 3,
            "successful_media": 2,
            "generated_thumbnails": 6,
            "retryable_failed_media": 1,
            "terminal_failed_media": 0,
        }

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_media_thumbnail_generation_job", fake_run_job)

    result = runner.invoke(main, ["aps", "generate-media-thumbnails"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert (
        "thumbnail generation finished: pending_media=3 successful_media=2 "
        "generated_thumbnails=6 retryable_failed_media=1 terminal_failed_media=0"
        in result.output
    )


def test_aps_index_image_search_thumbnails_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {
            "pending_thumbnails": 4,
            "successful_thumbnails": 3,
            "failed_thumbnails": 1,
        }

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_image_search_index_job", fake_run_job)

    result = runner.invoke(main, ["aps", "index-image-search-thumbnails"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert (
        "image search index finished: pending_thumbnails=4 successful_thumbnails=3 failed_thumbnails=1"
        in result.output
    )


def test_aps_optimize_image_search_index_command_runs_job(monkeypatch):
    called = {"job": 0}

    def fake_run_job():
        called["job"] += 1
        return {"compacted": True}

    runner = CliRunner()
    monkeypatch.setattr("src.start.aps.run_image_search_optimize_job", fake_run_job)

    result = runner.invoke(main, ["aps", "optimize-image-search-index"])

    assert result.exit_code == 0
    assert called["job"] == 1
    assert "image search optimize finished: compacted=True" in result.output


def test_build_scheduler_registers_actor_subscription_sync_job(monkeypatch):
    monkeypatch.setattr("src.start.aps.settings.scheduler.timezone", "Asia/Shanghai")
    monkeypatch.setattr("src.start.aps.settings.scheduler.actor_subscription_sync_cron", "0 2 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_heat_cron", "15 0 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.download_task_sync_cron", "*/15 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.download_task_auto_import_cron", "*/10 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.movie_collection_sync_cron", "0 1 * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.media_thumbnail_cron", "*/5 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_index_cron", "*/10 * * * *")
    monkeypatch.setattr("src.start.aps.settings.scheduler.image_search_optimize_cron", "0 */6 * * *")

    scheduler = build_scheduler()
    actor_job = scheduler.get_job("actor_subscription_sync")
    heat_job = scheduler.get_job("movie_heat_update")
    download_sync_job = scheduler.get_job("download_task_sync")
    auto_import_job = scheduler.get_job("download_task_auto_import")
    collection_job = scheduler.get_job("movie_collection_sync")
    thumbnail_job = scheduler.get_job("media_thumbnail_generation")
    image_search_index_job = scheduler.get_job("image_search_index")
    image_search_optimize_job = scheduler.get_job("image_search_optimize")

    assert actor_job is not None
    assert heat_job is not None
    assert download_sync_job is not None
    assert auto_import_job is not None
    assert collection_job is not None
    assert thumbnail_job is not None
    assert image_search_index_job is not None
    assert image_search_optimize_job is not None
    assert str(actor_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='2', minute='0']"
    assert str(collection_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='1', minute='0']"
    assert str(heat_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='0', minute='15']"
    assert str(download_sync_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/15']"
    assert str(auto_import_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(thumbnail_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/5']"
    assert str(image_search_index_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='*', minute='*/10']"
    assert str(image_search_optimize_job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='*/6', minute='0']"
    assert scheduler.timezone.key == "Asia/Shanghai"


def test_run_movie_heat_update_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.MovieHeatService.update_movie_heat",
        lambda: {"candidate_count": 2, "updated_count": 2, "formula_version": "v1"},
    )

    from src.start.aps import run_movie_heat_update_job

    result = run_movie_heat_update_job()

    assert result == {"candidate_count": 2, "updated_count": 2, "formula_version": "v1"}
    assert events == ["ready", "movie-heat-update"]


def test_run_download_task_sync_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.DownloadSyncService.sync_all_clients",
        lambda self: {"total_clients": 1, "scanned_count": 2, "created_count": 1, "updated_count": 1, "unchanged_count": 0},
    )

    from src.start.aps import run_download_task_sync_job

    result = run_download_task_sync_job()

    assert result["total_clients"] == 1
    assert events == ["ready", "download-task-sync"]


def test_run_image_search_index_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.ImageSearchIndexService.index_pending_thumbnails",
        lambda self: {"pending_thumbnails": 1, "successful_thumbnails": 1, "failed_thumbnails": 0},
    )

    from src.start.aps import run_image_search_index_job

    result = run_image_search_index_job()

    assert result == {"pending_thumbnails": 1, "successful_thumbnails": 1, "failed_thumbnails": 0}
    assert events == ["ready", "image-search-index"]


def test_run_image_search_optimize_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.ImageSearchIndexService.optimize_index",
        lambda self: {"compacted": True},
    )

    from src.start.aps import run_image_search_optimize_job

    result = run_image_search_optimize_job()

    assert result == {"compacted": True}
    assert events == ["ready", "image-search-optimize"]


def test_run_movie_collection_sync_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.MovieCollectionService.sync_movie_collections",
        lambda: {
            "total_movies": 3,
            "matched_count": 1,
            "updated_to_collection_count": 1,
            "updated_to_single_count": 1,
            "unchanged_count": 1,
        },
    )

    from src.start.aps import run_movie_collection_sync_job

    result = run_movie_collection_sync_job()

    assert result == {
        "total_movies": 3,
        "matched_count": 1,
        "updated_to_collection_count": 1,
        "updated_to_single_count": 1,
        "unchanged_count": 1,
    }
    assert events == ["ready", "movie-collection-sync"]


def test_run_media_thumbnail_generation_job_ensures_database_before_running(monkeypatch):
    events = []

    def fake_ensure_database_ready():
        events.append("ready")

    def fake_run_logged_task(task_name, func):
        events.append(task_name)
        return func()

    monkeypatch.setattr("src.start.aps._ensure_database_ready", fake_ensure_database_ready)
    monkeypatch.setattr("src.start.aps.run_logged_task", fake_run_logged_task)
    monkeypatch.setattr(
        "src.start.aps.MediaThumbnailService.generate_pending_thumbnails",
        lambda: {
            "pending_media": 1,
            "successful_media": 1,
            "generated_thumbnails": 2,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
        },
    )

    from src.start.aps import run_media_thumbnail_generation_job

    result = run_media_thumbnail_generation_job()

    assert result["generated_thumbnails"] == 2
    assert events == ["ready", "media-thumbnail-generation"]


def test_run_logged_task_logs_success_and_failure():
    events = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

        def exception(self, message, *args):
            events.append(("exception", message.format(*args) if args else message))

    def fake_get_task_logger(task_name: str):
        assert task_name == "actor-subscription-sync"
        return FakeLogger()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("src.scheduler.runner.get_task_logger", fake_get_task_logger)
    try:
        result = run_logged_task("actor-subscription-sync", lambda: {"ok": True})
        assert result == {"ok": True}
        assert events[0][0] == "info"
        assert events[-1][0] == "info"

        events.clear()
        with pytest.raises(RuntimeError):
            run_logged_task("actor-subscription-sync", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert events[0][0] == "info"
        assert events[-1][0] == "exception"
    finally:
        monkeypatch.undo()


def test_run_logged_task_writes_nested_logs_to_task_sink(monkeypatch, tmp_path):
    from src.scheduler import logging as scheduler_logging

    _TASK_SINKS.clear()
    _TASK_LEVELS.clear()
    monkeypatch.setattr("src.scheduler.logging.settings.scheduler.log_dir", str(tmp_path))
    monkeypatch.setattr("src.scheduler.logging.settings.logging.level", "INFO")

    try:
        run_logged_task("actor-subscription-sync", lambda: logger.info("nested flow log"))
        content = (tmp_path / "actor-subscription-sync.log").read_text(encoding="utf-8")
    finally:
        for sink_id in list(_TASK_SINKS.values()):
            scheduler_logging.logger.remove(sink_id)
        _TASK_SINKS.clear()
        _TASK_LEVELS.clear()

    assert "Scheduler task started" in content
    assert "nested flow log" in content
    assert "Scheduler task finished" in content


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
