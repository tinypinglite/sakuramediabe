"""migrate-movie-subtitles CLI 层测试 —— mock service，只验证参数透传和 exit code。"""

from __future__ import annotations

from click.testing import CliRunner

from src.service.catalog.movie_subtitle_unify_migration_service import MigrationStats
from src.start.commands import main


def test_dry_run_reports_stats_without_writes(monkeypatch):
    """--dry-run 透传到 service.run(dry_run=True)。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        captured["has_progress"] = progress_callback is not None
        stats = MigrationStats()
        stats.subtitles_scanned = 8
        stats.subtitles_migrated = 6
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieSubtitleUnifyMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-subtitles", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True
    assert captured["has_progress"] is True
    assert "dry_run=true" in result.output
    assert "'subtitles_migrated': 6" in result.output


def test_default_run_passes_dry_run_false(monkeypatch):
    """缺省时 dry_run=False。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        return MigrationStats()

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieSubtitleUnifyMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-subtitles"])

    assert result.exit_code == 0
    assert captured["dry_run"] is False


def test_exit_code_zero_even_when_some_subtitles_failed(monkeypatch):
    """failed / data_lost 都不让 CLI 挂掉。"""
    def fake_run(*, dry_run, progress_callback):
        stats = MigrationStats()
        stats.subtitles_scanned = 50
        stats.subtitles_failed = 2
        stats.subtitles_data_lost = 1
        stats.failed_paths = ["/lib/jav/AAA-1/1/AAA-1.srt"]
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieSubtitleUnifyMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-subtitles"])

    assert result.exit_code == 0
    assert "'subtitles_failed': 2" in result.output
    assert "'subtitles_data_lost': 1" in result.output
