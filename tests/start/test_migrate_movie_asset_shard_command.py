"""migrate-movie-asset-shard CLI 层测试 —— mock service，只验证参数透传和 exit code。"""

from __future__ import annotations

from click.testing import CliRunner

from src.service.catalog.movie_asset_shard_migration_service import MigrationStats
from src.start.commands import main


def test_dry_run_reports_stats_without_writes(monkeypatch):
    """--dry-run 透传到 service.run(dry_run=True)。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        captured["has_progress"] = progress_callback is not None
        stats = MigrationStats()
        stats.directories_scanned = 12
        stats.directories_sharded = 12
        stats.images_rewritten = 30
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieAssetShardMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-asset-shard", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True
    assert captured["has_progress"] is True
    assert "dry_run=true" in result.output
    assert "'directories_sharded': 12" in result.output


def test_default_run_passes_dry_run_false(monkeypatch):
    """缺省时 dry_run=False。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        return MigrationStats()

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieAssetShardMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-asset-shard"])

    assert result.exit_code == 0
    assert captured["dry_run"] is False


def test_exit_code_zero_even_when_conflicts_recorded(monkeypatch):
    """冲突/意外条目都记进 stats 但不让 CLI 挂掉。"""
    def fake_run(*, dry_run, progress_callback):
        stats = MigrationStats()
        stats.images_scanned = 100
        stats.images_conflict_skipped = 2
        stats.directories_conflict_skipped = 1
        stats.unexpected_root_entries = 3
        stats.conflict_origins = ["movies/AAA-1/cover.jpg"]
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.MovieAssetShardMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-movie-asset-shard"])

    assert result.exit_code == 0
    assert "'images_conflict_skipped': 2" in result.output
    assert "'directories_conflict_skipped': 1" in result.output
    assert "'unexpected_root_entries': 3" in result.output
