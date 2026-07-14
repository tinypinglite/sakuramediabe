from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.model import BackgroundTaskRun, Image, MediaLibrary, SchemaMigration, VideoImportJob
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
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            javdb_id VARCHAR(64) NOT NULL UNIQUE,
            movie_number VARCHAR(255) NOT NULL UNIQUE,
            title TEXT NOT NULL,
            release_date TIMESTAMP NULL,
            duration_minutes INTEGER NOT NULL DEFAULT 0,
            score DOUBLE PRECISION NOT NULL DEFAULT 0,
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
            is_collection BOOLEAN NOT NULL DEFAULT FALSE,
            is_collection_overridden BOOLEAN NOT NULL DEFAULT FALSE,
            is_subscribed BOOLEAN NOT NULL DEFAULT FALSE,
            subscribed_at TIMESTAMP NULL,
            "desc" TEXT NOT NULL DEFAULT '',
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
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', %s, %s, %s, %s
        )
        """,
        (javdb_id, movie_number, movie_number, series_name),
    )


def _create_import_job_table_missing_transfer_mode(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE import_job (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            source_path VARCHAR(1024) NOT NULL,
            library_id INTEGER NOT NULL,
            download_task_id INTEGER NULL,
            task_run_id INTEGER NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'pending',
            imported_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            failed_files TEXT NOT NULL DEFAULT '[]',
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL
        )
        """
    )


def _insert_legacy_import_job(test_db, source_path: str, state: str = "failed"):
    test_db.execute_sql(
        """
        INSERT INTO import_job (created_at, updated_at, source_path, library_id, state)
        VALUES ('2026-05-01 00:00:00', '2026-05-01 00:00:00', %s, 1, %s)
        """,
        (source_path, state),
    )


def _create_media_library_table_without_account_key(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE media_library (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            backend VARCHAR(32) NOT NULL DEFAULT 'local',
            backend_config TEXT NOT NULL
        )
        """
    )


def _create_import_job_table_with_cloud_columns(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE import_job (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            source_path VARCHAR(1024) NOT NULL,
            source_cid VARCHAR(255) NULL,
            library_id INTEGER NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'pending',
            transfer_mode VARCHAR(32) NOT NULL DEFAULT 'auto',
            imported_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            failed_files TEXT NOT NULL DEFAULT '[]'
        )
        """
    )


def _create_actor_table_missing_subscribed_at(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE actor (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            javdb_id VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            alias_name TEXT NOT NULL DEFAULT '',
            profile_image_id INTEGER NULL,
            javdb_type INTEGER NOT NULL DEFAULT 0,
            gender INTEGER NOT NULL DEFAULT 0,
            is_subscribed BOOLEAN NOT NULL DEFAULT FALSE,
            subscribed_movies_synced_at TIMESTAMP NULL,
            subscribed_movies_full_synced_at TIMESTAMP NULL
        )
        """
    )


def _insert_legacy_actor(test_db, *, javdb_id: str, name: str, created_at: str, is_subscribed: bool):
    test_db.execute_sql(
        """
        INSERT INTO actor (
            created_at, updated_at, javdb_id, name, is_subscribed
        ) VALUES (
            %s, %s, %s, %s, %s
        )
        """,
        (created_at, created_at, javdb_id, name, is_subscribed),
    )


def _schema_migration_names(test_db):
    # 迁移断言要显式绑定当前测试数据库，避免读取到别的用例遗留连接。
    with test_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return [item.name for item in SchemaMigration.select().order_by(SchemaMigration.id)]


def _movie_foreign_keys(test_db):
    return [
        {
            "table": row[0],
            "from": row[1],
            "to": row[2],
            "on_delete": row[3],
        }
        for row in test_db.execute_sql(
            """
            SELECT
                ccu.table_name,
                kcu.column_name,
                ccu.column_name,
                rc.delete_rule
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
                AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints AS rc
                ON rc.constraint_name = tc.constraint_name
                AND rc.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_schema = current_schema()
                AND tc.table_name = 'movie'
            """
        ).fetchall()
    ]


def test_run_pending_migrations_extracts_movie_series_from_supported_legacy_schema(test_db, monkeypatch):
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
    migration_names = _schema_migration_names(test_db)
    assert "20260421_01_add_movie_title_zh" in migration_names
    assert "20260424_01_extract_movie_series" in migration_names
    assert "20260508_01_add_daily_recommendations" in migration_names
    assert test_db.table_exists("daily_recommendation_item")


def test_run_pending_migrations_is_idempotent(test_db):
    _create_movie_table_missing_title_zh(test_db)

    first_summary = run_pending_migrations(test_db)
    second_summary = run_pending_migrations(test_db)

    first_migration_names = _schema_migration_names(test_db)
    assert "20260508_01_add_daily_recommendations" in first_migration_names
    assert second_summary.applied_count == 0
    assert second_summary.skipped_count == len(second_summary.executed)
    assert _schema_migration_names(test_db) == first_migration_names


def test_run_pending_migrations_skips_when_target_table_is_missing(test_db):
    summary = run_pending_migrations(test_db)

    daily_execution = next(
        item for item in summary.executed if item.name == "20260508_01_add_daily_recommendations"
    )
    assert daily_execution.applied is False
    assert "20260508_01_add_daily_recommendations" not in _schema_migration_names(test_db)
    moment_execution = next(
        item for item in summary.executed if item.name == "20260508_02_add_moment_recommendations"
    )
    assert moment_execution.applied is False
    assert "20260508_02_add_moment_recommendations" not in _schema_migration_names(test_db)
    clip_execution = next(
        item for item in summary.executed if item.name == "20260613_02_add_media_clip_tables"
    )
    assert clip_execution.applied is False
    assert not test_db.table_exists("media_clip")


def test_run_pending_migrations_creates_media_clip_tables_on_empty_database(test_db):
    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(test_db)

    clip_execution = next(
        item for item in summary.executed if item.name == "20260613_02_add_media_clip_tables"
    )
    assert clip_execution.applied is True
    assert test_db.table_exists("media_clip")
    assert test_db.table_exists("clip_collection")
    assert test_db.table_exists("clip_collection_item")


def test_run_pending_migrations_creates_video_import_job_on_existing_database(test_db):
    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)
    # 模拟尚未应用当天迁移的库：依赖表都在，但还没有 video_import_job，下次启动 migrate 应自动补建。
    test_db.drop_tables([VideoImportJob])
    assert not test_db.table_exists("video_import_job")

    summary = run_pending_migrations(test_db)

    execution = next(
        item for item in summary.executed if item.name == "20260613_01_add_videos_and_decouple_media"
    )
    assert execution.applied is True
    assert test_db.table_exists("video_import_job")
    assert "20260613_01_add_videos_and_decouple_media" in _schema_migration_names(test_db)


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
                    MigrationExecution(name="20260429_01_add_actor_subscribed_at", applied=True),
                    MigrationExecution(name="20260508_01_add_daily_recommendations", applied=True),
                    MigrationExecution(name="20260508_02_add_moment_recommendations", applied=True),
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
                MigrationExecution(name="20260429_01_add_actor_subscribed_at", applied=False),
                MigrationExecution(name="20260508_01_add_daily_recommendations", applied=False),
                MigrationExecution(name="20260508_02_add_moment_recommendations", applied=False),
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
    assert "applied: 20260429_01_add_actor_subscribed_at" in result.output
    assert "applied: 20260508_01_add_daily_recommendations" in result.output
    assert "applied: 20260508_02_add_moment_recommendations" in result.output
    assert "migrate finished: applied=6 skipped=0 total=6" in result.output
    assert events == ["db.connect", ("run", legacy_database), "db.ready", ("run", ready_database)]


def _create_legacy_system_notification_table(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE system_notification (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            category VARCHAR(32) NOT NULL,
            level VARCHAR(32) NOT NULL,
            title VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            read_at TIMESTAMP NULL,
            archived_at TIMESTAMP NULL,
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
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', %s, %s, %s, %s
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


def test_run_pending_migrations_adds_actor_subscribed_at_and_backfills_created_at(test_db):
    _create_actor_table_missing_subscribed_at(test_db)
    _insert_legacy_actor(
        test_db,
        javdb_id="ActorA1",
        name="三上悠亚",
        created_at="2026-03-08 09:00:00",
        is_subscribed=True,
    )
    _insert_legacy_actor(
        test_db,
        javdb_id="ActorA2",
        name="河北彩花",
        created_at="2026-03-10 09:00:00",
        is_subscribed=False,
    )

    run_pending_migrations(test_db)

    columns = {column.name for column in test_db.get_columns("actor")}
    indexed_columns = [column for index in test_db.get_indexes("actor") for column in index.columns]
    rows = test_db.execute_sql(
        "SELECT javdb_id, subscribed_at FROM actor ORDER BY javdb_id"
    ).fetchall()

    assert "subscribed_at" in columns
    assert "subscribed_at" in indexed_columns
    assert rows == [("ActorA1", datetime(2026, 3, 8, 9, 0, 0)), ("ActorA2", None)]


def test_run_pending_migrations_adds_import_job_transfer_mode_with_default(test_db):
    _create_import_job_table_missing_transfer_mode(test_db)
    _insert_legacy_import_job(test_db, source_path="/mnt/incoming", state="failed")

    run_pending_migrations(test_db)

    columns = {column.name for column in test_db.get_columns("import_job")}
    rows = test_db.execute_sql(
        "SELECT source_path, transfer_mode FROM import_job ORDER BY id"
    ).fetchall()

    assert "transfer_mode" in columns
    # 历史行回填为默认值 auto，默认值由 Peewee 字段渲染而非手写字面量。
    assert rows == [("/mnt/incoming", "auto")]


def test_run_pending_migrations_supports_empty_database_after_create_tables(test_db):
    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(test_db)
    movie_columns = {column.name for column in test_db.get_columns("movie")}
    actor_columns = {column.name for column in test_db.get_columns("actor")}

    # 空库先按当前模型建表后，迁移执行结果至少要保持最终 schema 正确。
    assert "title_zh" in movie_columns
    assert "series_id" in movie_columns
    assert "series_name" not in movie_columns
    assert test_db.table_exists("movie_series")
    assert "subscribed_at" in actor_columns
    daily_execution = next(
        item for item in summary.executed if item.name == "20260508_01_add_daily_recommendations"
    )
    assert daily_execution.applied is True
    assert test_db.table_exists("daily_recommendation_item")
    moment_execution = next(
        item for item in summary.executed if item.name == "20260508_02_add_moment_recommendations"
    )
    assert moment_execution.applied is True
    assert test_db.table_exists("moment_recommendation")


def _create_legacy_notification_table_with_archived_index(test_db):
    test_db.execute_sql(
        """
        CREATE TABLE system_notification (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            category VARCHAR(32) NOT NULL,
            title VARCHAR(255) NOT NULL,
            content TEXT NOT NULL,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            read_at TIMESTAMP NULL,
            archived_at TIMESTAMP NULL,
            related_task_run_id INTEGER NULL,
            related_resource_type VARCHAR(64) NULL,
            related_resource_id INTEGER NULL
        )
        """
    )
    test_db.execute_sql("CREATE INDEX system_notification_category ON system_notification(category)")
    # 旧 Peewee 模型 archived_at=index=True 自动生成的是无下划线模型名前缀索引名。
    test_db.execute_sql(
        "CREATE INDEX systemnotification_archived_at ON system_notification(archived_at)"
    )


def test_run_pending_migrations_drops_notification_archived_at(test_db):
    _create_legacy_notification_table_with_archived_index(test_db)
    test_db.execute_sql(
        """
        INSERT INTO system_notification (
            created_at, updated_at, category, title, content
        ) VALUES (
            '2026-06-01 00:00:00', '2026-06-01 00:00:00', 'reminder', '新影片', ''
        )
        """
    )

    run_pending_migrations(test_db)

    columns = {column.name for column in test_db.get_columns("system_notification")}
    indexes = {index.name for index in test_db.get_indexes("system_notification")}
    indexed_columns = [
        column for index in test_db.get_indexes("system_notification") for column in index.columns
    ]
    rows = test_db.execute_sql(
        "SELECT title, category FROM system_notification ORDER BY id"
    ).fetchall()

    assert "archived_at" not in columns
    assert "systemnotification_archived_at" not in indexes
    assert "archived_at" not in indexed_columns
    # 删列不应破坏历史数据。
    assert rows == [("新影片", "reminder")]
    assert "20260609_01_drop_notification_archived_at" in _schema_migration_names(test_db)


def _create_legacy_media_table_not_null(test_db):
    # 旧版 media 表：movie_number 为 NOT NULL 外键列，且没有 video_item_id。
    test_db.execute_sql(
        """
        CREATE TABLE media (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            movie_number VARCHAR(255) NOT NULL,
            library_id INTEGER NULL,
            path VARCHAR(1024) NOT NULL UNIQUE,
            storage_mode VARCHAR(32) NULL,
            resolution VARCHAR(32) NULL,
            content_fingerprint VARCHAR(255) NULL,
            file_size_bytes INTEGER NOT NULL DEFAULT 0,
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            video_info TEXT NULL,
            special_tags VARCHAR(255) NOT NULL DEFAULT '普通',
            valid BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )


def _column_is_nullable(test_db, table_name: str, column_name: str) -> bool:
    for column in test_db.get_columns(table_name):
        if column.name == column_name:
            return column.null
    raise AssertionError(f"column not found: {table_name}.{column_name}")


def test_run_pending_migrations_adds_media_backend_locator(test_db):
    """20260714_01：media 补 backend_locator、path 放松可空、(library_id, backend_locator) 唯一索引。"""
    _create_legacy_media_table_not_null(test_db)
    test_db.create_tables([Image, MediaLibrary, BackgroundTaskRun])
    test_db.execute_sql(
        """
        INSERT INTO media (created_at, updated_at, movie_number, path, content_fingerprint)
        VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'ABP-001', '/lib/abp-001.mp4', 'fp-1')
        """
    )

    run_pending_migrations(test_db)

    media_columns = {column.name for column in test_db.get_columns("media")}
    assert "backend_locator" in media_columns
    assert _column_is_nullable(test_db, "media", "backend_locator") is True
    assert _column_is_nullable(test_db, "media", "path") is True
    # 复合唯一索引已建出。
    unique_index_columns = [
        tuple(index.columns) for index in test_db.get_indexes("media") if index.unique
    ]
    assert ("library_id", "backend_locator") in unique_index_columns
    # 存量本地行 backend_locator 保持 NULL，数据完好。
    rows = test_db.execute_sql(
        "SELECT movie_number, path, backend_locator FROM media ORDER BY id"
    ).fetchall()
    assert rows == [("ABP-001", "/lib/abp-001.mp4", None)]


def test_run_pending_migrations_adds_import_job_source_cid(test_db):
    """20260714_02：import_job 补 source_cid 可空列，存量行保持 NULL。"""
    _create_import_job_table_missing_transfer_mode(test_db)
    _insert_legacy_import_job(test_db, source_path="/mnt/incoming", state="completed")

    run_pending_migrations(test_db)

    columns = {column.name for column in test_db.get_columns("import_job")}
    assert "source_cid" in columns
    rows = test_db.execute_sql(
        "SELECT source_path, source_cid FROM import_job ORDER BY id"
    ).fetchall()
    assert rows == [("/mnt/incoming", None)]


def test_run_pending_migrations_backfills_cloud115_account_key(test_db):
    _create_media_library_table_without_account_key(test_db)
    test_db.execute_sql(
        """
        INSERT INTO media_library (
            created_at, updated_at, name, backend, backend_config
        ) VALUES (
            '2026-07-14 00:00:00', '2026-07-14 00:00:00', 'cloud', 'cloud115',
            '{"cookies":"UID=12345678_A1_1700000000; CID=abc","root_cid":"1"}'
        )
        """
    )

    run_pending_migrations(test_db)

    row = test_db.execute_sql(
        "SELECT backend_account_key FROM media_library WHERE name = 'cloud'"
    ).fetchone()
    assert row == ("cloud115:12345678",)
    indexes = [index for index in test_db.get_indexes("media_library") if index.unique]
    assert any(tuple(index.columns) == ("backend_account_key",) for index in indexes)


def test_cloud115_account_key_migration_rejects_existing_duplicates(test_db):
    _create_media_library_table_without_account_key(test_db)
    for name in ("cloud-a", "cloud-b"):
        test_db.execute_sql(
            """
            INSERT INTO media_library (
                created_at, updated_at, name, backend, backend_config
            ) VALUES (
                '2026-07-14 00:00:00', '2026-07-14 00:00:00', %s, 'cloud115',
                '{"cookies":"UID=12345678_A1_1700000000; CID=abc","root_cid":"1"}'
            )
            """,
            (name,),
        )

    with pytest.raises(ValueError, match="cloud115_media_library_account_duplicate"):
        run_pending_migrations(test_db)


def test_run_pending_migrations_normalizes_legacy_cloud115_move_jobs(test_db):
    _create_import_job_table_with_cloud_columns(test_db)
    test_db.execute_sql(
        """
        INSERT INTO import_job (
            created_at, updated_at, source_path, source_cid, library_id, transfer_mode
        ) VALUES
            ('2026-07-14', '2026-07-14', 'cloud', 'cid-1', 1, 'move'),
            ('2026-07-14', '2026-07-14', '/local', NULL, 1, 'move')
        """
    )

    run_pending_migrations(test_db)

    rows = test_db.execute_sql(
        "SELECT source_cid, transfer_mode FROM import_job ORDER BY id"
    ).fetchall()
    assert rows == [("cid-1", "cleanup-source"), (None, "move")]


def test_run_pending_migrations_decouples_media_movie_and_adds_video_item(test_db, monkeypatch):
    _create_legacy_media_table_not_null(test_db)
    # 真实老库里 image / media_library / background_task_run 都是既有核心表，videos 域新表建表时
    # 外键引用它们；PostgreSQL 要求被引用表先存在，故显式建出还原真实前置状态。
    test_db.create_tables([Image, MediaLibrary, BackgroundTaskRun])
    test_db.execute_sql(
        """
        INSERT INTO media (created_at, updated_at, movie_number, path, content_fingerprint)
        VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'ABP-001', '/lib/abp-001.mp4', 'fp-1')
        """
    )

    run_pending_migrations(test_db)

    media_columns = {column.name for column in test_db.get_columns("media")}
    existing_tables = set(test_db.get_tables())

    # video_item 新增列存在，movie_number 放松为可空。
    assert "video_item_id" in media_columns
    assert _column_is_nullable(test_db, "media", "movie_number") is True
    # videos 域新表已建出（含异步导入作业表，无标签、无 person 系列表）。
    assert {
        "video_item",
        "video_collection",
        "video_collection_item",
        "video_import_job",
    }.issubset(existing_tables)
    assert "person" not in existing_tables
    assert "video_item_person" not in existing_tables
    assert "video_tag" not in existing_tables
    assert "video_item_tag" not in existing_tables
    # 既有 JAV 媒体数据完好。
    rows = test_db.execute_sql(
        "SELECT movie_number, path, video_item_id FROM media ORDER BY id"
    ).fetchall()
    assert rows == [("ABP-001", "/lib/abp-001.mp4", None)]
    assert "20260613_01_add_videos_and_decouple_media" in _schema_migration_names(test_db)
