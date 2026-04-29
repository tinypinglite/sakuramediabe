from pathlib import Path

from click.testing import CliRunner

from src.config.config import DatabaseEngine
from src.model import SchemaMigration
from src.start.commands import main
from src.start.migrations.runner import (
    MigrationExecution,
    MigrationRunSummary,
    _load_migration_module,
    run_pending_migrations,
)
from tests.conftest import TEST_MODELS


def _create_movie_table_missing_title_zh(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE movie (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            javdb_id VARCHAR(64) NOT NULL UNIQUE,
            movie_number VARCHAR(255) NOT NULL UNIQUE,
            title TEXT NOT NULL,
            release_date DATETIME NULL,
            duration_minutes INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            score_number INTEGER NOT NULL DEFAULT 0,
            watched_count INTEGER NOT NULL DEFAULT 0,
            cover_image_id INTEGER NULL,
            thin_cover_image_id INTEGER NULL,
            summary TEXT NOT NULL DEFAULT '',
            series_name VARCHAR(255) NULL,
            maker_name VARCHAR(255) NULL,
            director_name VARCHAR(255) NULL,
            want_watch_count INTEGER NOT NULL DEFAULT 0,
            comment_count INTEGER NOT NULL DEFAULT 0,
            heat INTEGER NOT NULL DEFAULT 0,
            is_collection INTEGER NOT NULL DEFAULT 0,
            is_collection_overridden INTEGER NOT NULL DEFAULT 0,
            is_subscribed INTEGER NOT NULL DEFAULT 0,
            subscribed_at DATETIME NULL,
            desc TEXT NOT NULL DEFAULT '',
            desc_zh TEXT NOT NULL DEFAULT '',
            extra TEXT NULL
        )
        """
    )
    # 旧模型中 series_name 带 index=True，真实旧库会保留该索引。
    test_db.execute_sql("CREATE INDEX movie_series_name ON movie(series_name)")


def _insert_legacy_movie(test_db, movie_number: str, javdb_id: str, series_name: str | None):
    test_db.execute_sql(
        """
        INSERT INTO movie (
            created_at, updated_at, javdb_id, movie_number, title, series_name
        ) VALUES (
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', ?, ?, ?, ?
        )
        """,
        (javdb_id, movie_number, movie_number, series_name),
    )


def _schema_migration_names(test_db):
    # 迁移断言要显式绑定当前测试数据库，避免读取到别的用例遗留连接。
    with test_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return [item.name for item in SchemaMigration.select().order_by(SchemaMigration.id)]


def _movie_foreign_keys(test_db):
    return [
        {
            "table": row[2],
            "from": row[3],
            "to": row[4],
            "on_delete": row[6],
        }
        for row in test_db.execute_sql("PRAGMA foreign_key_list(movie)").fetchall()
    ]


def test_run_pending_migrations_extracts_movie_series_from_supported_legacy_schema(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)
    # 当前受支持的老用户 schema 只缺少 title_zh。
    _create_movie_table_missing_title_zh(test_db)
    _insert_legacy_movie(test_db, "ABP-001", "javdb-001", " A 系列 ")
    _insert_legacy_movie(test_db, "ABP-002", "javdb-002", "A 系列")
    _insert_legacy_movie(test_db, "ABP-003", "javdb-003", "   ")
    _insert_legacy_movie(test_db, "ABP-004", "javdb-004", None)

    summary = run_pending_migrations(test_db)

    movie_columns = {column.name for column in test_db.get_columns("movie")}
    movie_indexes = {index.name for index in test_db.get_indexes("movie")}
    movie_foreign_keys = _movie_foreign_keys(test_db)
    series_rows = test_db.execute_sql("SELECT id, name FROM movie_series ORDER BY id").fetchall()
    movie_rows = test_db.execute_sql(
        "SELECT movie_number, series_id FROM movie ORDER BY movie_number"
    ).fetchall()

    assert "title_zh" in movie_columns
    assert "series_id" in movie_columns
    assert "series_name" not in movie_columns
    assert "movie_series_name" not in movie_indexes
    assert "movie_series_id" in movie_indexes
    assert {
        "table": "movie_series",
        "from": "series_id",
        "to": "id",
        "on_delete": "SET NULL",
    } in movie_foreign_keys
    assert series_rows == [(1, "A 系列")]
    assert movie_rows == [("ABP-001", 1), ("ABP-002", 1), ("ABP-003", None), ("ABP-004", None)]
    test_db.execute_sql("DELETE FROM movie_series WHERE id = 1")
    movie_rows_after_series_delete = test_db.execute_sql(
        "SELECT movie_number, series_id FROM movie ORDER BY movie_number"
    ).fetchall()
    assert movie_rows_after_series_delete == [
        ("ABP-001", None),
        ("ABP-002", None),
        ("ABP-003", None),
        ("ABP-004", None),
    ]
    assert summary.applied_count == 2
    # system_notification 表在该 legacy schema 下不存在，对应迁移会主动跳过。
    assert summary.skipped_count == 1
    assert _schema_migration_names(test_db) == [
        "20260421_01_add_movie_title_zh",
        "20260424_01_extract_movie_series",
    ]


def test_run_pending_migrations_is_idempotent(test_db):
    _create_movie_table_missing_title_zh(test_db)

    first_summary = run_pending_migrations(test_db)
    second_summary = run_pending_migrations(test_db)

    assert first_summary.applied_count == 2
    assert first_summary.skipped_count == 1
    assert second_summary.applied_count == 0
    assert second_summary.skipped_count == 3
    assert _schema_migration_names(test_db) == [
        "20260421_01_add_movie_title_zh",
        "20260424_01_extract_movie_series",
    ]


def test_run_pending_migrations_skips_when_target_table_is_missing(test_db):
    summary = run_pending_migrations(test_db)

    assert summary.applied_count == 0
    assert summary.skipped_count == 3
    assert _schema_migration_names(test_db) == []


def test_load_migration_module_uses_package_import():
    module = _load_migration_module(
        Path("src/start/migrations/versions/20260421_01_add_movie_title_zh.py")
    )

    assert module.name == "20260421_01_add_movie_title_zh"
    assert callable(module.migrate)


def test_migrate_command_runs_pending_migrations_without_initdb(monkeypatch):
    events = []
    legacy_database = object()
    ready_database = object()

    def fake_run_pending_migrations(database):
        events.append(("run", database))
        if database is legacy_database:
            return MigrationRunSummary(
                executed=[
                    MigrationExecution(name="20260421_01_add_movie_title_zh", applied=True),
                    MigrationExecution(name="20260424_01_extract_movie_series", applied=True),
                    MigrationExecution(
                        name="20260426_01_merge_notification_category_level",
                        applied=True,
                    ),
                ]
            )
        return MigrationRunSummary(
            executed=[
                MigrationExecution(name="20260421_01_add_movie_title_zh", applied=False),
                MigrationExecution(name="20260424_01_extract_movie_series", applied=False),
                MigrationExecution(
                    name="20260426_01_merge_notification_category_level",
                    applied=False,
                ),
            ]
        )

    monkeypatch.setattr(
        "src.start.commands._connect_database_for_migration",
        lambda: events.append("db.connect") or legacy_database,
    )
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: events.append("db.ready") or ready_database)
    monkeypatch.setattr(
        "src.start.migrations.run_pending_migrations",
        fake_run_pending_migrations,
    )
    monkeypatch.setattr(
        "src.start.commands.create_tables",
        lambda: (_ for _ in ()).throw(AssertionError("should not call create_tables")),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["migrate"])

    assert result.exit_code == 0, result.output
    assert "applied: 20260421_01_add_movie_title_zh" in result.output
    assert "applied: 20260424_01_extract_movie_series" in result.output
    assert "applied: 20260426_01_merge_notification_category_level" in result.output
    assert "migrate finished: applied=3 skipped=0 total=3" in result.output
    assert events == ["db.connect", ("run", legacy_database), "db.ready", ("run", ready_database)]


def _create_legacy_system_notification_table(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE system_notification (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            category VARCHAR(32) NOT NULL,
            level VARCHAR(32) NOT NULL,
            title VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            read_at DATETIME NULL,
            archived_at DATETIME NULL,
            related_task_run_id INTEGER NULL,
            related_resource_type VARCHAR(64) NULL,
            related_resource_id INTEGER NULL
        )
        """
    )
    test_db.execute_sql("CREATE INDEX system_notification_category ON system_notification(category)")
    # 旧 Peewee 模型 index=True 自动生成的是无下划线模型名前缀索引名。
    test_db.execute_sql("CREATE INDEX systemnotification_level ON system_notification(level)")


def _insert_legacy_notification(test_db, *, category: str, level: str, title: str):
    test_db.execute_sql(
        """
        INSERT INTO system_notification (
            created_at, updated_at, category, level, title, content
        ) VALUES (
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', ?, ?, ?, ?
        )
        """,
        (category, level, title, ""),
    )


def test_run_pending_migrations_merges_notification_category_and_level(test_db):
    _create_legacy_system_notification_table(test_db)
    _insert_legacy_notification(test_db, category="exception", level="error", title="任务失败")
    _insert_legacy_notification(test_db, category="result", level="warning", title="部分失败")
    _insert_legacy_notification(test_db, category="result", level="info", title="任务完成")
    _insert_legacy_notification(test_db, category="reminder", level="info", title="新影片")

    run_pending_migrations(test_db)

    columns = {column.name for column in test_db.get_columns("system_notification")}
    indexes = {index.name for index in test_db.get_indexes("system_notification")}
    indexed_columns = [column for index in test_db.get_indexes("system_notification") for column in index.columns]
    rows = test_db.execute_sql(
        "SELECT title, category FROM system_notification ORDER BY id"
    ).fetchall()

    assert "level" not in columns
    assert "systemnotification_level" not in indexes
    assert "level" not in indexed_columns
    assert rows == [
        ("任务失败", "error"),
        ("部分失败", "warning"),
        ("任务完成", "info"),
        ("新影片", "reminder"),
    ]


def test_run_pending_migrations_supports_empty_database_after_create_tables(test_db):
    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(test_db)
    movie_columns = {column.name for column in test_db.get_columns("movie")}

    # 空库先按当前模型建表后，迁移执行结果至少要保持最终 schema 正确。
    assert "title_zh" in movie_columns
    assert "series_id" in movie_columns
    assert "series_name" not in movie_columns
    assert test_db.table_exists("movie_series")
    assert summary.applied_count == 3
    assert [item.name for item in summary.executed] == [
        "20260421_01_add_movie_title_zh",
        "20260424_01_extract_movie_series",
        "20260426_01_merge_notification_category_level",
    ]
