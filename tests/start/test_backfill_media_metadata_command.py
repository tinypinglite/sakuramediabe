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
