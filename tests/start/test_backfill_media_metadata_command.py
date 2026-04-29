import pytest
from click.testing import CliRunner

from src.start.commands import main


@pytest.fixture(autouse=True)
def _patch_database_prepare(monkeypatch):
    # 命令输出测试不依赖真实数据库初始化，统一 stub 掉准备流程。
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)


def test_backfill_movie_thin_cover_images_command_outputs_stats(monkeypatch):
    class FakeMovieThinCoverBackfillService:
        def backfill_missing_thin_cover_images(self):
            return {
                "scanned_movies": 6,
                "updated_movies": 4,
                "skipped_movies": 1,
                "failed_movies": 1,
            }

    monkeypatch.setattr(
        "src.start.commands.MovieThinCoverBackfillService",
        FakeMovieThinCoverBackfillService,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["backfill-movie-thin-cover-images"])

    assert result.exit_code == 0
    assert "scanned_movies=6" in result.output
    assert "updated_movies=4" in result.output
    assert "skipped_movies=1" in result.output
    assert "failed_movies=1" in result.output


def test_scan_media_files_command_outputs_stats(monkeypatch):
    class FakeMediaFileScanService:
        def scan_media_files(self):
            return {
                "scanned_media": 10,
                "updated_media": 4,
                "skipped_media": 3,
                "failed_media": 1,
                "invalidated_media": 1,
                "revived_media": 1,
            }

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MediaFileScanService",
        FakeMediaFileScanService,
    )

    runner = CliRunner()
    result = runner.invoke(main, ["scan-media-files"])

    assert result.exit_code == 0
    assert "scanned_media=10" in result.output
    assert "updated_media=4" in result.output
    assert "skipped_media=3" in result.output
    assert "failed_media=1" in result.output
    assert "invalidated_media=1" in result.output
    assert "revived_media=1" in result.output


def test_cleanup_movie_subtitle_fetch_history_command_outputs_stats(monkeypatch):
    class FakeDeleteQuery:
        def __init__(self, deleted_count: int):
            self.deleted_count = deleted_count

        def where(self, *_args, **_kwargs):
            return self

        def execute(self):
            return self.deleted_count

    class FakeBackgroundTaskRun:
        task_key = "movie_subtitle_fetch"

        @staticmethod
        def delete():
            return FakeDeleteQuery(3)

    class FakeResourceTaskState:
        task_key = "movie_subtitle_fetch"

        @staticmethod
        def delete():
            return FakeDeleteQuery(5)

    monkeypatch.setattr("src.start.commands.BackgroundTaskRun", FakeBackgroundTaskRun)
    monkeypatch.setattr("src.start.commands.ResourceTaskState", FakeResourceTaskState)

    runner = CliRunner()
    result = runner.invoke(main, ["cleanup-movie-subtitle-fetch-history"])

    assert result.exit_code == 0
    assert "deleted_task_runs=3" in result.output
    assert "deleted_resource_task_states=5" in result.output
