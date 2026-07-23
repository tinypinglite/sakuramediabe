"""migrate-plot-layout CLI 层测试 —— mock service，只验证 CLI 参数透传和 exit code。"""

from __future__ import annotations

from click.testing import CliRunner

from src.service.catalog.plot_layout_migration_service import MigrationStats
from src.start.commands import main


def test_dry_run_reports_stats_without_writes(monkeypatch):
    """--dry-run 透传到 service.run(dry_run=True)。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        captured["has_progress"] = progress_callback is not None
        stats = MigrationStats()
        stats.images_scanned = 12
        stats.images_migrated = 10  # 反映"如果真跑会做什么"
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.PlotLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-plot-layout", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True
    assert captured["has_progress"] is True
    assert "dry_run=true" in result.output
    assert "'images_migrated': 10" in result.output


def test_default_run_passes_dry_run_false(monkeypatch):
    """缺省时 dry_run=False。"""
    captured = {}

    def fake_run(*, dry_run, progress_callback):
        captured["dry_run"] = dry_run
        return MigrationStats()

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.PlotLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-plot-layout"])

    assert result.exit_code == 0
    assert captured["dry_run"] is False


def test_exit_code_zero_even_when_some_images_failed(monkeypatch):
    """状态矩阵里 images_failed / images_data_lost / images_conflict_skipped 都不让 CLI 挂掉。"""
    def fake_run(*, dry_run, progress_callback):
        stats = MigrationStats()
        stats.images_scanned = 100
        stats.images_failed = 2
        stats.images_data_lost = 1
        stats.images_conflict_skipped = 3
        stats.failed_origins = ["movies/AAA-1/plots/0.jpg", "movies/BBB-2/plots/1.jpg"]
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.PlotLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-plot-layout"])

    assert result.exit_code == 0
    assert "'images_failed': 2" in result.output
    assert "'images_data_lost': 1" in result.output
    assert "'images_conflict_skipped': 3" in result.output
