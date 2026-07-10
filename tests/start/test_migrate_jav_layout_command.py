"""migrate-jav-layout CLI 层测试 —— mock service，只验证 CLI 参数透传和 exit code。"""

from __future__ import annotations

from click.testing import CliRunner

from src.service.playback.jav_layout_migration_service import MigrationStats
from src.start.commands import main


def test_dry_run_reports_stats_without_writes(monkeypatch):
    """--dry-run 透传到 service.run(dry_run=True)。"""
    captured = {}

    def fake_run(*, library_id, dry_run, progress_callback):
        captured["library_id"] = library_id
        captured["dry_run"] = dry_run
        captured["has_progress"] = progress_callback is not None
        stats = MigrationStats()
        stats.libraries_scanned = 2
        stats.media_migrated = 5  # 反映"如果真跑会做什么"
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.JavLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-jav-layout", "--dry-run"])

    assert result.exit_code == 0
    assert captured["dry_run"] is True
    assert captured["library_id"] is None
    assert captured["has_progress"] is True
    # 汇总输出里带 dry_run=true 和 stats 关键字段
    assert "dry_run=true" in result.output
    assert "'media_migrated': 5" in result.output


def test_library_id_filter_passed_through(monkeypatch):
    """--library-id 透传到 service.run(library_id=...)。"""
    captured = {}

    def fake_run(*, library_id, dry_run, progress_callback):
        captured["library_id"] = library_id
        captured["dry_run"] = dry_run
        return MigrationStats()

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.JavLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-jav-layout", "--library-id", "7"])

    assert result.exit_code == 0
    assert captured["library_id"] == 7
    assert captured["dry_run"] is False


def test_exit_code_zero_even_when_some_media_failed(monkeypatch):
    """状态矩阵里 media_failed / media_data_lost / media_rolled_back 都不让 CLI 挂掉 —— 让用户看 stats 手工排。"""
    def fake_run(*, library_id, dry_run, progress_callback):
        stats = MigrationStats()
        stats.libraries_scanned = 1
        stats.media_failed = 2
        stats.media_data_lost = 1
        stats.media_rolled_back = 3
        stats.failed_files = ["/library/A.mp4", "/library/B.mp4"]
        return stats

    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.start.commands.JavLayoutMigrationService.run",
        fake_run,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["migrate-jav-layout"])

    assert result.exit_code == 0
    # stats 内容出现在输出里，用户能一眼看到问题规模
    assert "'media_failed': 2" in result.output
    assert "'media_data_lost': 1" in result.output
    assert "'media_rolled_back': 3" in result.output
