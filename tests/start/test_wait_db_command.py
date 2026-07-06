from click.testing import CliRunner

from src.config.config import Database, settings
from src.start.commands import main


def test_wait_db_succeeds_when_database_is_reachable(test_db):
    runner = CliRunner()

    result = runner.invoke(main, ["wait-db", "--timeout", "10", "--interval", "0.1"])

    assert result.exit_code == 0, result.output
    assert "database is ready" in result.output


def test_wait_db_fails_fast_when_database_is_unreachable(monkeypatch):
    # 指向必然拒绝连接的端口，超时设 0 让首次失败后立即到达截止时间。
    monkeypatch.setattr(
        settings,
        "database",
        Database(url="postgresql://nobody:nothing@127.0.0.1:1/unreachable"),
    )
    runner = CliRunner()

    result = runner.invoke(main, ["wait-db", "--timeout", "0", "--interval", "0.1"])

    assert result.exit_code != 0
    assert "database not ready after" in result.output
