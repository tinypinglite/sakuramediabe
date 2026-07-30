import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from peewee import IntegrityError

from src.model import BackgroundTaskRun, Image, MediaLibrary, SchemaMigration, VideoImportJob
from src.start.commands import main
from src.start.migrations.runner import (
    MigrationExecution,
    MigrationRunSummary,
    _load_migration_module,
    run_pending_migrations,
)
from tests.conftest import TEST_MODELS


def _create_movie_table_missing_title_zh(clean_db):
    clean_db.execute_sql(
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
    clean_db.execute_sql("CREATE INDEX movie_series_name ON movie(series_name)")


def _insert_legacy_movie(clean_db, movie_number: str, javdb_id: str, series_name: str | None):
    clean_db.execute_sql(
        """
        INSERT INTO movie (
            created_at, updated_at, javdb_id, movie_number, title, series_name
        ) VALUES (
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', %s, %s, %s, %s
        )
        """,
        (javdb_id, movie_number, movie_number, series_name),
    )


def _create_import_job_table_missing_transfer_mode(clean_db):
    clean_db.execute_sql(
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


def _insert_legacy_import_job(clean_db, source_path: str, state: str = "failed"):
    clean_db.execute_sql(
        """
        INSERT INTO import_job (created_at, updated_at, source_path, library_id, state)
        VALUES ('2026-05-01 00:00:00', '2026-05-01 00:00:00', %s, 1, %s)
        """,
        (source_path, state),
    )


def _create_media_library_table_without_account_key(clean_db):
    clean_db.execute_sql(
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


def _create_legacy_media_library_table_with_root_path(clean_db):
    """20260713_01 迁移前的表结构：只有 name + root_path（无 backend/backend_config）。"""
    clean_db.execute_sql(
        """
        CREATE TABLE media_library (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            root_path VARCHAR(1024) NOT NULL UNIQUE
        )
        """
    )


def _create_import_job_table_with_cloud_columns(clean_db):
    clean_db.execute_sql(
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


def _create_actor_table_missing_subscribed_at(clean_db):
    clean_db.execute_sql(
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


def _insert_legacy_actor(clean_db, *, javdb_id: str, name: str, created_at: str, is_subscribed: bool):
    clean_db.execute_sql(
        """
        INSERT INTO actor (
            created_at, updated_at, javdb_id, name, is_subscribed
        ) VALUES (
            %s, %s, %s, %s, %s
        )
        """,
        (created_at, created_at, javdb_id, name, is_subscribed),
    )


def _schema_migration_names(clean_db):
    # 迁移断言要显式绑定当前测试数据库，避免读取到别的用例遗留连接。
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return [item.name for item in SchemaMigration.select().order_by(SchemaMigration.id)]


def _pin_migrations_as_applied(clean_db, *names):
    """把与本用例无关的清理型迁移预先标记为已应用。

    20260730_02/04/05 会整表清空导入作业、秒传、通知历史。老用例从空的
    SchemaMigration 起跑全量迁移，末尾这些清理会把它们插入的断言样本一并删掉，
    使断言失去被测迁移的针对性——这里把它们钉成已应用来隔离。
    """
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        clean_db.create_tables([SchemaMigration], safe=True)
        for name in names:
            SchemaMigration.get_or_create(name=name)


def _movie_foreign_keys(clean_db):
    return [
        {
            "table": row[0],
            "from": row[1],
            "to": row[2],
            "on_delete": row[3],
        }
        for row in clean_db.execute_sql(
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


def test_run_pending_migrations_extracts_movie_series_from_supported_legacy_schema(clean_db, monkeypatch):
    # 当前受支持的老用户 schema 只缺少 title_zh。
    _create_movie_table_missing_title_zh(clean_db)
    _insert_legacy_movie(clean_db, "ABP-001", "javdb-001", " A 系列 ")
    _insert_legacy_movie(clean_db, "ABP-002", "javdb-002", "A 系列")
    _insert_legacy_movie(clean_db, "ABP-003", "javdb-003", "   ")
    _insert_legacy_movie(clean_db, "ABP-004", "javdb-004", None)

    summary = run_pending_migrations(clean_db)

    movie_columns = {column.name for column in clean_db.get_columns("movie")}
    movie_indexes = {index.name for index in clean_db.get_indexes("movie")}
    movie_foreign_keys = _movie_foreign_keys(clean_db)
    series_rows = clean_db.execute_sql("SELECT id, name FROM movie_series ORDER BY id").fetchall()
    movie_rows = clean_db.execute_sql(
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
    clean_db.execute_sql("DELETE FROM movie_series WHERE id = 1")
    movie_rows_after_series_delete = clean_db.execute_sql(
        "SELECT movie_number, series_id FROM movie ORDER BY movie_number"
    ).fetchall()
    assert movie_rows_after_series_delete == [
        ("ABP-001", None),
        ("ABP-002", None),
        ("ABP-003", None),
        ("ABP-004", None),
    ]
    migration_names = _schema_migration_names(clean_db)
    assert "20260421_01_add_movie_title_zh" in migration_names
    assert "20260424_01_extract_movie_series" in migration_names
    assert "20260508_01_add_daily_recommendations" in migration_names
    assert clean_db.table_exists("daily_recommendation_item")


def test_run_pending_migrations_is_idempotent(clean_db):
    _create_movie_table_missing_title_zh(clean_db)

    first_summary = run_pending_migrations(clean_db)
    second_summary = run_pending_migrations(clean_db)

    first_migration_names = _schema_migration_names(clean_db)
    assert "20260508_01_add_daily_recommendations" in first_migration_names
    assert second_summary.applied_count == 0
    assert second_summary.skipped_count == len(second_summary.executed)
    assert _schema_migration_names(clean_db) == first_migration_names


def test_run_pending_migrations_skips_when_target_table_is_missing(clean_db):
    summary = run_pending_migrations(clean_db)

    daily_execution = next(
        item for item in summary.executed if item.name == "20260508_01_add_daily_recommendations"
    )
    assert daily_execution.applied is False
    assert "20260508_01_add_daily_recommendations" not in _schema_migration_names(clean_db)
    moment_execution = next(
        item for item in summary.executed if item.name == "20260508_02_add_moment_recommendations"
    )
    assert moment_execution.applied is False
    assert "20260508_02_add_moment_recommendations" not in _schema_migration_names(clean_db)
    clip_execution = next(
        item for item in summary.executed if item.name == "20260613_02_add_media_clip_tables"
    )
    assert clip_execution.applied is False
    assert not clean_db.table_exists("media_clip")


def test_run_pending_migrations_records_source_cid_migrations_after_fresh_initdb(clean_db):
    """全新安装：initdb 建出终态 schema 后再跑 migrate，02/05 也应记入 SchemaMigration。

    修复前这两条迁移在"列已存在"分支抛 SkipMigration，全新环境永远缺这两条审计记录；
    修复后走 return 分支，SchemaMigration 会写入，`applied=True`，重跑走 continue。
    """
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(clean_db)

    source_cid_execution = next(
        item for item in summary.executed
        if item.name == "20260714_02_add_import_job_source_cid"
    )
    assert source_cid_execution.applied is True
    cloud_source_execution = next(
        item for item in summary.executed
        if item.name == "20260714_05_add_video_import_job_cloud_source"
    )
    assert cloud_source_execution.applied is True

    recorded_names = _schema_migration_names(clean_db)
    assert "20260714_02_add_import_job_source_cid" in recorded_names
    assert "20260714_05_add_video_import_job_cloud_source" in recorded_names


def test_run_pending_migrations_creates_media_clip_tables_on_empty_database(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(clean_db)

    clip_execution = next(
        item for item in summary.executed if item.name == "20260613_02_add_media_clip_tables"
    )
    assert clip_execution.applied is True
    assert clean_db.table_exists("media_clip")
    assert clean_db.table_exists("clip_collection")
    assert clean_db.table_exists("clip_collection_item")


def test_run_pending_migrations_creates_video_import_job_on_existing_database(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟尚未应用当天迁移的库：依赖表都在，但还没有 video_import_job，下次启动 migrate 应自动补建。
    clean_db.drop_tables([VideoImportJob])
    assert not clean_db.table_exists("video_import_job")

    summary = run_pending_migrations(clean_db)

    execution = next(
        item for item in summary.executed if item.name == "20260613_01_add_videos_and_decouple_media"
    )
    assert execution.applied is True
    assert clean_db.table_exists("video_import_job")
    assert "20260613_01_add_videos_and_decouple_media" in _schema_migration_names(clean_db)


def test_run_pending_migrations_adds_video_import_job_cloud_sources_idempotently(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    _pin_migrations_as_applied(clean_db, "20260730_02_wipe_cloud115_download_import_history")
    library = MediaLibrary.create(name="local-videos", backend="local", backend_config={})
    job = VideoImportJob.create(source_path="/mnt/incoming", library=library)
    # 模拟迁移中途只加了一列：重跑时必须只补缺失列，并保留历史作业。
    clean_db.execute_sql('ALTER TABLE "video_import_job" DROP COLUMN "source_fid"')

    summary = run_pending_migrations(clean_db)

    execution = next(
        item
        for item in summary.executed
        if item.name == "20260714_05_add_video_import_job_cloud_source"
    )
    assert execution.applied is True
    columns = {column.name: column for column in clean_db.get_columns("video_import_job")}
    assert columns["source_cid"].null is True
    assert columns["source_fid"].null is True
    row = clean_db.execute_sql(
        "SELECT id, source_path, source_cid, source_fid FROM video_import_job WHERE id = %s",
        (job.id,),
    ).fetchone()
    assert row == (job.id, "/mnt/incoming", None, None)

    second_summary = run_pending_migrations(clean_db)
    second_execution = next(
        item
        for item in second_summary.executed
        if item.name == "20260714_05_add_video_import_job_cloud_source"
    )
    assert second_execution.applied is False


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


def _create_legacy_system_notification_table(clean_db):
    clean_db.execute_sql(
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
    clean_db.execute_sql("CREATE INDEX system_notification_category ON system_notification(category)")
    # 旧 Peewee 模型 index=True 自动生成的是无下划线模型名前缀索引名。
    clean_db.execute_sql("CREATE INDEX systemnotification_level ON system_notification(level)")


def _insert_legacy_notification(clean_db, *, category: str, level: str, title: str):
    clean_db.execute_sql(
        """
        INSERT INTO system_notification (
            created_at, updated_at, category, level, title, content
        ) VALUES (
            '2024-01-01 00:00:00', '2024-01-01 00:00:00', %s, %s, %s, %s
        )
        """,
        (category, level, title, ""),
    )


def test_run_pending_migrations_merges_notification_category_and_level(clean_db):
    _create_legacy_system_notification_table(clean_db)
    _pin_migrations_as_applied(clean_db, "20260730_05_wipe_system_notifications")
    _insert_legacy_notification(clean_db, category="exception", level="error", title="任务失败")
    _insert_legacy_notification(clean_db, category="result", level="warning", title="部分失败")
    _insert_legacy_notification(clean_db, category="result", level="info", title="任务完成")
    _insert_legacy_notification(clean_db, category="reminder", level="info", title="新影片")

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("system_notification")}
    indexes = {index.name for index in clean_db.get_indexes("system_notification")}
    indexed_columns = [column for index in clean_db.get_indexes("system_notification") for column in index.columns]
    rows = clean_db.execute_sql(
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


def test_run_pending_migrations_adds_actor_subscribed_at_and_backfills_created_at(clean_db):
    _create_actor_table_missing_subscribed_at(clean_db)
    _insert_legacy_actor(
        clean_db,
        javdb_id="ActorA1",
        name="三上悠亚",
        created_at="2026-03-08 09:00:00",
        is_subscribed=True,
    )
    _insert_legacy_actor(
        clean_db,
        javdb_id="ActorA2",
        name="河北彩花",
        created_at="2026-03-10 09:00:00",
        is_subscribed=False,
    )

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("actor")}
    indexed_columns = [column for index in clean_db.get_indexes("actor") for column in index.columns]
    rows = clean_db.execute_sql(
        "SELECT javdb_id, subscribed_at FROM actor ORDER BY javdb_id"
    ).fetchall()

    assert "subscribed_at" in columns
    assert "subscribed_at" in indexed_columns
    assert rows == [("ActorA1", datetime(2026, 3, 8, 9, 0, 0)), ("ActorA2", None)]


def test_run_pending_migrations_adds_import_job_transfer_mode_with_default(clean_db):
    _create_import_job_table_missing_transfer_mode(clean_db)
    _insert_legacy_import_job(clean_db, source_path="/mnt/incoming", state="failed")

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("import_job")}
    rows = clean_db.execute_sql(
        "SELECT source_path, transfer_mode FROM import_job ORDER BY id"
    ).fetchall()

    assert "transfer_mode" in columns
    # 历史行回填为默认值 auto，默认值由 Peewee 字段渲染而非手写字面量。
    assert rows == [("/mnt/incoming", "auto")]


def test_run_pending_migrations_supports_empty_database_after_create_tables(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    summary = run_pending_migrations(clean_db)
    movie_columns = {column.name for column in clean_db.get_columns("movie")}
    actor_columns = {column.name for column in clean_db.get_columns("actor")}

    # 空库先按当前模型建表后，迁移执行结果至少要保持最终 schema 正确。
    assert "title_zh" in movie_columns
    assert "series_id" in movie_columns
    assert "series_name" not in movie_columns
    assert clean_db.table_exists("movie_series")
    assert "subscribed_at" in actor_columns
    daily_execution = next(
        item for item in summary.executed if item.name == "20260508_01_add_daily_recommendations"
    )
    assert daily_execution.applied is True
    assert clean_db.table_exists("daily_recommendation_item")
    moment_execution = next(
        item for item in summary.executed if item.name == "20260508_02_add_moment_recommendations"
    )
    assert moment_execution.applied is True
    assert clean_db.table_exists("moment_recommendation")
    rapid_upload_execution = next(
        item
        for item in summary.executed
        if item.name == "20260716_01_add_media_rapid_upload_jobs"
    )
    assert rapid_upload_execution.applied is True
    assert clean_db.table_exists("media_rapid_upload_batch")
    assert clean_db.table_exists("media_rapid_upload_item")


def _create_legacy_notification_table_with_archived_index(clean_db):
    clean_db.execute_sql(
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
    clean_db.execute_sql("CREATE INDEX system_notification_category ON system_notification(category)")
    # 旧 Peewee 模型 archived_at=index=True 自动生成的是无下划线模型名前缀索引名。
    clean_db.execute_sql(
        "CREATE INDEX systemnotification_archived_at ON system_notification(archived_at)"
    )


def test_run_pending_migrations_drops_notification_archived_at(clean_db):
    _create_legacy_notification_table_with_archived_index(clean_db)
    _pin_migrations_as_applied(clean_db, "20260730_05_wipe_system_notifications")
    clean_db.execute_sql(
        """
        INSERT INTO system_notification (
            created_at, updated_at, category, title, content
        ) VALUES (
            '2026-06-01 00:00:00', '2026-06-01 00:00:00', 'reminder', '新影片', ''
        )
        """
    )

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("system_notification")}
    indexes = {index.name for index in clean_db.get_indexes("system_notification")}
    indexed_columns = [
        column for index in clean_db.get_indexes("system_notification") for column in index.columns
    ]
    rows = clean_db.execute_sql(
        "SELECT title, category FROM system_notification ORDER BY id"
    ).fetchall()

    assert "archived_at" not in columns
    assert "systemnotification_archived_at" not in indexes
    assert "archived_at" not in indexed_columns
    # 删列不应破坏历史数据。
    assert rows == [("新影片", "reminder")]
    assert "20260609_01_drop_notification_archived_at" in _schema_migration_names(clean_db)


def _create_legacy_media_table_not_null(clean_db):
    # 旧版 media 表：movie_number 为 NOT NULL 外键列，且没有 video_item_id。
    clean_db.execute_sql(
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


def _column_is_nullable(clean_db, table_name: str, column_name: str) -> bool:
    for column in clean_db.get_columns(table_name):
        if column.name == column_name:
            return column.null
    raise AssertionError(f"column not found: {table_name}.{column_name}")


def test_run_pending_migrations_migrates_root_path_to_backend_config(clean_db):
    """20260713_01：旧 media_library.root_path 迁移到 backend + backend_config JSON，且旧列被删除。

    这是本次改动里“爆炸半径”最大的迁移（新增 backend/backend_config 两列 + 回填 + 删列），
    覆盖存量本地库的完整迁移路径：无 backend 列 → 补列 → JSON 回填 → 删旧列。
    """
    _create_legacy_media_library_table_with_root_path(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO media_library (created_at, updated_at, name, root_path) VALUES
            ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'Main', '/media/library/main'),
            ('2026-01-02 00:00:00', '2026-01-02 00:00:00', 'Archive', '/media/library/archive')
        """
    )

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("media_library")}
    assert "backend" in columns
    assert "backend_config" in columns
    # 旧 root_path 列已被删除（此后应用代码只能通过 backend_config 读到路径）。
    assert "root_path" not in columns
    # backend 列已加 NOT NULL 约束（存量行由方言引擎默认值 'local' 回填）。
    assert _column_is_nullable(clean_db, "media_library", "backend") is False
    assert _column_is_nullable(clean_db, "media_library", "backend_config") is False

    rows = clean_db.execute_sql(
        "SELECT name, backend, backend_config FROM media_library ORDER BY name"
    ).fetchall()

    def _as_dict(raw):
        return raw if isinstance(raw, dict) else json.loads(raw)

    assert len(rows) == 2
    by_name = {row[0]: (row[1], _as_dict(row[2])) for row in rows}
    assert by_name["Archive"] == ("local", {"root_path": "/media/library/archive"})
    assert by_name["Main"] == ("local", {"root_path": "/media/library/main"})


def test_run_pending_migrations_adds_media_backend_locator(clean_db):
    """20260714_01：media 补 backend_locator、path 放松可空、(library_id, backend_locator) 唯一索引。"""
    _create_legacy_media_table_not_null(clean_db)
    clean_db.create_tables([Image, MediaLibrary, BackgroundTaskRun])
    clean_db.execute_sql(
        """
        INSERT INTO media (created_at, updated_at, movie_number, path, content_fingerprint)
        VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'ABP-001', '/lib/abp-001.mp4', 'fp-1')
        """
    )

    run_pending_migrations(clean_db)

    media_columns = {column.name for column in clean_db.get_columns("media")}
    assert "backend_locator" in media_columns
    assert _column_is_nullable(clean_db, "media", "backend_locator") is True
    assert _column_is_nullable(clean_db, "media", "path") is True
    # 复合唯一索引已建出。
    unique_index_columns = [
        tuple(index.columns) for index in clean_db.get_indexes("media") if index.unique
    ]
    assert ("library_id", "backend_locator") in unique_index_columns
    # 存量本地行 backend_locator 保持 NULL，数据完好。
    rows = clean_db.execute_sql(
        "SELECT movie_number, path, backend_locator FROM media ORDER BY id"
    ).fetchall()
    assert rows == [("ABP-001", "/lib/abp-001.mp4", None)]


def test_run_pending_migrations_adds_import_job_source_cid(clean_db):
    """20260714_02：import_job 补 source_cid 可空列，存量行保持 NULL。"""
    _create_import_job_table_missing_transfer_mode(clean_db)
    _insert_legacy_import_job(clean_db, source_path="/mnt/incoming", state="completed")

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("import_job")}
    assert "source_cid" in columns
    rows = clean_db.execute_sql(
        "SELECT source_path, source_cid FROM import_job ORDER BY id"
    ).fetchall()
    assert rows == [("/mnt/incoming", None)]


def test_run_pending_migrations_backfills_cloud115_account_key(clean_db):
    _create_media_library_table_without_account_key(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO media_library (
            created_at, updated_at, name, backend, backend_config
        ) VALUES (
            '2026-07-14 00:00:00', '2026-07-14 00:00:00', 'cloud', 'cloud115',
            '{"cookies":"UID=12345678_A1_1700000000; CID=abc","root_cid":"1"}'
        )
        """
    )

    run_pending_migrations(clean_db)

    row = clean_db.execute_sql(
        "SELECT backend_account_key FROM media_library WHERE name = 'cloud'"
    ).fetchone()
    assert row == ("cloud115:12345678",)
    indexes = [index for index in clean_db.get_indexes("media_library") if index.unique]
    assert any(tuple(index.columns) == ("backend_account_key",) for index in indexes)


def test_cloud115_account_key_migration_rejects_existing_duplicates(clean_db):
    _create_media_library_table_without_account_key(clean_db)
    for name in ("cloud-a", "cloud-b"):
        clean_db.execute_sql(
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
        run_pending_migrations(clean_db)


def test_run_pending_migrations_normalizes_legacy_cloud115_move_jobs(clean_db):
    _create_import_job_table_with_cloud_columns(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO import_job (
            created_at, updated_at, source_path, source_cid, library_id, transfer_mode
        ) VALUES
            ('2026-07-14', '2026-07-14', 'cloud', 'cid-1', 1, 'move'),
            ('2026-07-14', '2026-07-14', '/local', NULL, 1, 'move')
        """
    )

    run_pending_migrations(clean_db)

    rows = clean_db.execute_sql(
        "SELECT source_cid, transfer_mode FROM import_job ORDER BY id"
    ).fetchall()
    assert rows == [("cid-1", "cleanup-source"), (None, "move")]


def test_run_pending_migrations_decouples_media_movie_and_adds_video_item(clean_db, monkeypatch):
    _create_legacy_media_table_not_null(clean_db)
    # 真实老库里 image / media_library / background_task_run 都是既有核心表，videos 域新表建表时
    # 外键引用它们；PostgreSQL 要求被引用表先存在，故显式建出还原真实前置状态。
    clean_db.create_tables([Image, MediaLibrary, BackgroundTaskRun])
    clean_db.execute_sql(
        """
        INSERT INTO media (created_at, updated_at, movie_number, path, content_fingerprint)
        VALUES ('2026-01-01 00:00:00', '2026-01-01 00:00:00', 'ABP-001', '/lib/abp-001.mp4', 'fp-1')
        """
    )

    run_pending_migrations(clean_db)

    media_columns = {column.name for column in clean_db.get_columns("media")}
    existing_tables = set(clean_db.get_tables())

    # video_item 新增列存在，movie_number 放松为可空。
    assert "video_item_id" in media_columns
    assert _column_is_nullable(clean_db, "media", "movie_number") is True
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
    rows = clean_db.execute_sql(
        "SELECT movie_number, path, video_item_id FROM media ORDER BY id"
    ).fetchall()
    assert rows == [("ABP-001", "/lib/abp-001.mp4", None)]
    assert "20260613_01_add_videos_and_decouple_media" in _schema_migration_names(clean_db)


def _create_legacy_download_tables(clean_db):
    """kind / target_ref / 中间表出现之前的下载域 schema：qb 字段全 NOT NULL，indexer 单 FK。"""
    clean_db.execute_sql(
        """
        CREATE TABLE download_client (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            base_url VARCHAR(255) NOT NULL,
            username VARCHAR(255) NOT NULL,
            password VARCHAR(255) NOT NULL,
            client_save_path VARCHAR(1024) NOT NULL,
            local_root_path VARCHAR(1024) NOT NULL,
            media_library_id INTEGER NOT NULL
        )
        """
    )
    clean_db.execute_sql(
        """
        CREATE TABLE download_task (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            client_id INTEGER NOT NULL,
            movie_number VARCHAR(255),
            name VARCHAR(255) NOT NULL,
            info_hash VARCHAR(128) NOT NULL,
            save_path VARCHAR(1024) NOT NULL,
            progress DOUBLE PRECISION NOT NULL DEFAULT 0,
            download_state VARCHAR(32) NOT NULL,
            import_status VARCHAR(32) NOT NULL
        )
        """
    )
    clean_db.execute_sql(
        """
        CREATE TABLE indexer (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            url VARCHAR(1024) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            download_client_id INTEGER NOT NULL
        )
        """
    )


def test_run_pending_migrations_adds_download_client_kind_and_task_target_ref(clean_db):
    """20260714_06：download_client 补 kind 并回填 qbittorrent，qb 字段放开可空；download_task 补 target_ref。"""
    _create_legacy_download_tables(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO download_client (
            created_at, updated_at, name, base_url, username, password,
            client_save_path, local_root_path, media_library_id
        ) VALUES (
            '2026-07-14', '2026-07-14', 'qb-main', 'http://qb:8080', 'admin', 'secret',
            '/downloads', '/mnt/downloads', 1
        )
        """
    )
    clean_db.execute_sql(
        """
        INSERT INTO download_task (
            created_at, updated_at, client_id, name, info_hash, save_path,
            download_state, import_status
        ) VALUES ('2026-07-14', '2026-07-14', 1, 'ABP-001', 'hash-1', '/mnt/downloads/ABP-001',
            'completed', 'pending')
        """
    )

    run_pending_migrations(clean_db)

    client_columns = {column.name for column in clean_db.get_columns("download_client")}
    assert "kind" in client_columns
    # 存量行 kind 回填为 qbittorrent。
    rows = clean_db.execute_sql("SELECT name, kind FROM download_client ORDER BY id").fetchall()
    assert rows == [("qb-main", "qbittorrent")]
    # qb 专属字段全部放开为可空。
    for column_name in ("base_url", "username", "password", "client_save_path", "local_root_path"):
        assert _column_is_nullable(clean_db, "download_client", column_name) is True
    # download_task.target_ref 已补出，存量行保持 NULL。
    task_columns = {column.name for column in clean_db.get_columns("download_task")}
    assert "target_ref" in task_columns
    task_rows = clean_db.execute_sql("SELECT name, target_ref FROM download_task ORDER BY id").fetchall()
    assert task_rows == [("ABP-001", None)]


def test_run_pending_migrations_adds_partial_unique_cloud115_client_index(clean_db):
    _create_legacy_download_tables(clean_db)
    run_pending_migrations(clean_db)

    clean_db.execute_sql(
        """
        INSERT INTO download_client
            (created_at, updated_at, name, kind, media_library_id)
        VALUES
            (NOW(), NOW(), 'qb-a', 'qbittorrent', 1),
            (NOW(), NOW(), 'qb-b', 'qbittorrent', 1),
            (NOW(), NOW(), 'cloud-a', 'cloud115', 2)
        """
    )
    with pytest.raises(IntegrityError):
        clean_db.execute_sql(
            """
            INSERT INTO download_client
                (created_at, updated_at, name, kind, media_library_id)
            VALUES (NOW(), NOW(), 'cloud-b', 'cloud115', 2)
            """
        )

    index_names = {index.name for index in clean_db.get_indexes("download_client")}
    assert "download_client_cloud115_library_unique" in index_names
    assert "20260715_01_unique_cloud115_download_client_library" in _schema_migration_names(clean_db)


def test_cloud115_unique_client_migration_rejects_existing_duplicates(clean_db):
    """20260715_01：加唯一索引前发现存量脏数据（同一 media_library 已有多条 cloud115），必须报语义化错。

    直接构造已跑完 20260714_06 但未跑 20260715_01 的中间态：手写一张带 kind+media_library_id
    的 download_client 表，插入两条 cloud115 同库脏数据，然后单独调 15_01.migrate() 应该
    抛 ValueError 且携带 library_id，而非落到 Postgres 层裸 IntegrityError。
    """
    from importlib import import_module

    from playhouse.migrate import PostgresqlMigrator

    unique_migration_module = import_module(
        "src.start.migrations.versions.20260715_01_unique_cloud115_download_client_library"
    )

    clean_db.execute_sql(
        """
        CREATE TABLE download_client (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            kind VARCHAR(32) NOT NULL DEFAULT 'qbittorrent',
            media_library_id INTEGER NULL
        )
        """
    )
    clean_db.execute_sql(
        """
        INSERT INTO download_client (created_at, updated_at, name, kind, media_library_id)
        VALUES
            (NOW(), NOW(), 'cloud-a', 'cloud115', 42),
            (NOW(), NOW(), 'cloud-b', 'cloud115', 42)
        """
    )

    migrator = PostgresqlMigrator(clean_db)
    with pytest.raises(ValueError) as excinfo:
        unique_migration_module.migrate(clean_db, migrator)
    assert "cloud115_download_client_library_duplicate" in str(excinfo.value)
    assert "42" in str(excinfo.value)


def test_run_pending_migrations_adds_media_rapid_upload_item_failure_reason(clean_db):
    """20260720_01：老库补 failure_reason 列并按 error_message 精确回填。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    _pin_migrations_as_applied(clean_db, "20260730_04_wipe_rapid_upload_history")
    # 模拟老库：抹掉本迁移引入的新列，并把 SchemaMigration 里对应记录清掉让 runner 重跑。
    clean_db.execute_sql(
        'ALTER TABLE "media_rapid_upload_item" DROP COLUMN "failure_reason"'
    )
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.delete().where(
            SchemaMigration.name == "20260720_01_add_media_rapid_upload_item_failure_reason"
        ).execute()

    from src.model import (
        Media,
        MediaRapidUploadBatch,
        Movie,
    )

    library = MediaLibrary.create(
        name="lib", backend="local", backend_config={"root_path": "/lib"}
    )
    # Media 必须挂 movie 或 video_item；用 JAV 分支满足不变量。
    # (batch_id, media_id) 是唯一键，用 3 个不同 media 承载 3 种失败样本。
    movies = [
        Movie.create(movie_number=f"ABP-00{i}", javdb_id=f"a-00{i}", title=f"a{i}")
        for i in range(1, 4)
    ]
    medias = [
        Media.create(library=library, movie=movie, path=f"/lib/a{idx}.mp4")
        for idx, movie in enumerate(movies, start=1)
    ]
    batch = MediaRapidUploadBatch.create(target_library=library)
    # 三种老 error_message：not_hit / file_changed 应被回填；其它维持 null。
    clean_db.execute_sql(
        """
        INSERT INTO media_rapid_upload_item (
            created_at, updated_at, batch_id, media_id, action, state,
            source_path, source_size_bytes, source_mtime_ns, error_message
        ) VALUES
            (%s, %s, %s, %s, 'rapid_upload', 'failed',
             '/lib/a1.mp4', 0, 0, 'rapid upload status=not_hit'),
            (%s, %s, %s, %s, 'rapid_upload', 'failed',
             '/lib/a2.mp4', 0, 0, 'rapid upload status=file_changed'),
            (%s, %s, %s, %s, 'rapid_upload', 'failed',
             '/lib/a3.mp4', 0, 0, 'connection reset')
        """,
        (
            datetime(2026, 7, 20), datetime(2026, 7, 20), batch.id, medias[0].id,
            datetime(2026, 7, 20), datetime(2026, 7, 20), batch.id, medias[1].id,
            datetime(2026, 7, 20), datetime(2026, 7, 20), batch.id, medias[2].id,
        ),
    )

    run_pending_migrations(clean_db)

    columns = {column.name for column in clean_db.get_columns("media_rapid_upload_item")}
    assert "failure_reason" in columns
    rows = clean_db.execute_sql(
        "SELECT error_message, failure_reason FROM media_rapid_upload_item "
        "ORDER BY id"
    ).fetchall()
    assert rows == [
        ("rapid upload status=not_hit", "not_hit"),
        ("rapid upload status=file_changed", "file_changed"),
        ("connection reset", None),
    ]
    assert (
        "20260720_01_add_media_rapid_upload_item_failure_reason"
        in _schema_migration_names(clean_db)
    )


def test_run_pending_migrations_adds_media_rapid_upload_item_media_index(clean_db):
    """20260720_02：老库补 media_id 单列索引；幂等地跳过已存在索引。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟老库：抹掉 initdb 建出的 (media_id,) 单列索引并清 SchemaMigration 记录让 runner 重跑。
    # 索引名由 peewee 生成，不同版本可能不同，用列匹配定位后按实际名 DROP。
    stale_indexes = [
        index.name
        for index in clean_db.get_indexes("media_rapid_upload_item")
        if tuple(index.columns) == ("media_id",)
    ]
    for index_name in stale_indexes:
        clean_db.execute_sql(f'DROP INDEX IF EXISTS "{index_name}"')
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.delete().where(
            SchemaMigration.name == "20260720_02_add_media_rapid_upload_item_media_index"
        ).execute()

    run_pending_migrations(clean_db)

    indexed_columns = {
        tuple(index.columns)
        for index in clean_db.get_indexes("media_rapid_upload_item")
    }
    assert ("media_id",) in indexed_columns
    assert (
        "20260720_02_add_media_rapid_upload_item_media_index"
        in _schema_migration_names(clean_db)
    )

    # 幂等：再跑一次不应重复建索引出错。
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.delete().where(
            SchemaMigration.name == "20260720_02_add_media_rapid_upload_item_media_index"
        ).execute()
    run_pending_migrations(clean_db)


def test_run_pending_migrations_reverts_duration_based_collections(clean_db):
    """20260723_01：合集判定去掉时长阈值后，仅因时长被判为合集的影片恢复为单体；
    命中番号特征前缀的自动合集与手动覆盖的影片保持不变。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    from src.model import Movie

    # 前缀命中（默认特征含 OFJE）：自动合集，保持 True。
    Movie.create(movie_number="OFJE-100", javdb_id="j-100", title="t1",
                 is_collection=True, is_collection_overridden=False)
    # 无前缀命中、未覆盖：当初只能靠时长判定，应恢复为 False。
    Movie.create(movie_number="ABP-200", javdb_id="j-200", title="t2",
                 is_collection=True, is_collection_overridden=False)
    # 无前缀命中但手动覆盖：不受自动规则影响，保持 True。
    Movie.create(movie_number="ABP-300", javdb_id="j-300", title="t3",
                 is_collection=True, is_collection_overridden=True)
    # 本就是单体：不触碰。
    Movie.create(movie_number="ABP-400", javdb_id="j-400", title="t4",
                 is_collection=False, is_collection_overridden=False)
    # 规范化后命中前缀（小写 / PPV- 前缀）：保持 True，锁死与 service 判定一致的番号规范化。
    # 注意列里存的是原样（Movie.save() 只 strip），归一化只发生在迁移的匹配逻辑里。
    Movie.create(movie_number="ofje-500", javdb_id="j-500", title="t5",
                 is_collection=True, is_collection_overridden=False)
    Movie.create(movie_number="PPV-OFJE-777", javdb_id="j-777", title="t6",
                 is_collection=True, is_collection_overridden=False)

    run_pending_migrations(clean_db)

    rows = dict(
        clean_db.execute_sql(
            "SELECT movie_number, is_collection FROM movie ORDER BY movie_number"
        ).fetchall()
    )
    assert rows["OFJE-100"] is True
    assert rows["ABP-200"] is False
    assert rows["ABP-300"] is True
    assert rows["ABP-400"] is False
    assert rows["ofje-500"] is True
    assert rows["PPV-OFJE-777"] is True
    assert (
        "20260723_01_revert_duration_based_collections"
        in _schema_migration_names(clean_db)
    )


def test_run_pending_migrations_moves_indexer_binding_to_junction_table(clean_db):
    """20260714_07：indexer 单 FK 迁到 indexer_download_client 中间表并删旧列。"""
    _create_legacy_download_tables(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO download_client (
            created_at, updated_at, name, base_url, username, password,
            client_save_path, local_root_path, media_library_id
        ) VALUES (
            '2026-07-14', '2026-07-14', 'qb-main', 'http://qb:8080', 'admin', 'secret',
            '/downloads', '/mnt/downloads', 1
        )
        """
    )
    clean_db.execute_sql(
        """
        INSERT INTO indexer (created_at, updated_at, name, url, kind, download_client_id)
        VALUES ('2026-07-14', '2026-07-14', 'mteam', 'http://jackett/api', 'pt', 1)
        """
    )

    run_pending_migrations(clean_db)

    # 旧 FK 绑定被搬进中间表。
    assert clean_db.table_exists("indexer_download_client")
    link_rows = clean_db.execute_sql(
        "SELECT indexer_id, download_client_id FROM indexer_download_client ORDER BY id"
    ).fetchall()
    assert link_rows == [(1, 1)]
    # (indexer, download_client) 复合唯一索引已建出。
    unique_index_columns = [
        tuple(index.columns)
        for index in clean_db.get_indexes("indexer_download_client")
        if index.unique
    ]
    assert ("indexer_id", "download_client_id") in unique_index_columns
    # indexer 表的旧 FK 列已删除。
    indexer_columns = {column.name for column in clean_db.get_columns("indexer")}
    assert "download_client_id" not in indexer_columns
    assert "20260714_07_indexer_multi_client_binding" in _schema_migration_names(clean_db)


def test_run_pending_migrations_adds_movie_number_upper_index(clean_db):
    """存量库补 UPPER(movie_number) 函数索引；新库由 create_tables 直接建出。

    movie_number 列存 provider 规范原样、不做归一化改写，人工输入点查走
    UPPER(movie_number) 等值匹配（find_movie_by_number），全靠这个索引避免顺扫。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260728_01_add_movie_number_upper_index",),
    )
    # 模拟存量库：函数索引还不存在（create_tables 建出的先删掉）。
    clean_db.execute_sql("DROP INDEX IF EXISTS movie_movie_number_upper")

    run_pending_migrations(clean_db)

    assert clean_db.execute_sql(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'movie_movie_number_upper'"
        " AND schemaname = current_schema()"
    ).fetchone() is not None
    assert "20260728_01_add_movie_number_upper_index" in _schema_migration_names(clean_db)


def test_run_pending_migrations_drops_movie_similarity_table(clean_db):
    """Qdrant 接管相似度查询后，存量结果表不再保留。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql(
        'CREATE TABLE "movie_similarity" (id SERIAL PRIMARY KEY)'
    )

    run_pending_migrations(clean_db)

    assert not clean_db.table_exists("movie_similarity")
    assert "20260728_02_drop_movie_similarity" in _schema_migration_names(clean_db)


def test_run_pending_migrations_adds_task_queue_and_attempts(clean_db):
    """任务架构 Wave 0：存量库补队列列 / attempt 表 / 投影扩列，悬空 last_task_run_id 清零后外键化。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260729_01_add_task_queue_and_attempts",),
    )
    # 还原成存量库结构：删新列（依赖它们的索引随列一起消失）、删 attempt 表、删外键约束。
    clean_db.execute_sql(
        "ALTER TABLE background_task_run"
        " DROP COLUMN params, DROP COLUMN scheduled_at, DROP COLUMN lease_expires_at"
    )
    clean_db.execute_sql(
        "ALTER TABLE resource_task_state"
        " DROP COLUMN next_retry_at, DROP COLUMN error_code,"
        " DROP COLUMN retry_round, DROP COLUMN last_attempt_id"
    )
    clean_db.execute_sql('DROP TABLE "resource_task_attempt"')
    for legacy_constraint in (
        "resource_task_state_last_task_run_id_fkey",
        "resource_task_state_last_task_run_id_fk",
    ):
        clean_db.execute_sql(
            f"ALTER TABLE resource_task_state DROP CONSTRAINT IF EXISTS {legacy_constraint}"
        )

    # 存量数据：一条指向真实 run，一条悬空（活动清理删 run 后裸整数列的残留）。
    clean_db.execute_sql(
        "INSERT INTO background_task_run"
        " (created_at, updated_at, task_key, task_name, trigger_type, state, result_summary)"
        " VALUES (now(), now(), 'media_thumbnail_generation', '缩略图', 'scheduled', 'completed', '{}')"
    )
    run_id = clean_db.execute_sql("SELECT max(id) FROM background_task_run").fetchone()[0]
    clean_db.execute_sql(
        "INSERT INTO resource_task_state"
        " (created_at, updated_at, task_key, resource_type, resource_id, state,"
        "  attempt_count, last_task_run_id)"
        " VALUES"
        " (now(), now(), 'media_thumbnail_generation', 'media', 1, 'failed', 2, %s),"
        " (now(), now(), 'media_thumbnail_generation', 'media', 2, 'failed', 2, 999999)",
        (run_id,),
    )

    run_pending_migrations(clean_db)

    run_columns = {column.name for column in clean_db.get_columns("background_task_run")}
    assert {"params", "scheduled_at", "lease_expires_at"} <= run_columns
    state_columns = {column.name for column in clean_db.get_columns("resource_task_state")}
    assert {"next_retry_at", "error_code", "retry_round", "last_attempt_id"} <= state_columns
    assert clean_db.table_exists("resource_task_attempt")

    # 有效引用保留、悬空引用被清零。
    rows = clean_db.execute_sql(
        "SELECT resource_id, last_task_run_id FROM resource_task_state ORDER BY resource_id"
    ).fetchall()
    assert rows == [(1, run_id), (2, None)]

    # 外键约束与两个调度索引确实补上了。
    assert clean_db.execute_sql(
        """
        SELECT 1
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = current_schema()
          AND tc.table_name = 'resource_task_state'
          AND kcu.column_name = 'last_task_run_id'
        """
    ).fetchone() is not None
    run_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("background_task_run")
    }
    assert ("state", "scheduled_at") in run_indexed_columns
    state_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("resource_task_state")
    }
    assert ("task_key", "state", "next_retry_at") in state_indexed_columns
    assert "20260729_01_add_task_queue_and_attempts" in _schema_migration_names(clean_db)


def test_run_pending_migrations_wipes_task_run_history(clean_db):
    """任务架构 Wave 1 切换：清空 run/event 历史，外键引用自动置空（决策 #11）。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260729_02_wipe_task_run_history",),
    )
    clean_db.execute_sql(
        "INSERT INTO background_task_run"
        " (created_at, updated_at, task_key, task_name, trigger_type, state, result_summary)"
        " VALUES (now(), now(), 'ranking_sync', '排行榜', 'scheduled', 'completed', '{}')"
    )
    run_id = clean_db.execute_sql("SELECT max(id) FROM background_task_run").fetchone()[0]
    clean_db.execute_sql(
        "INSERT INTO system_event (created_at, updated_at, event_type, payload, emitted_at)"
        " VALUES (now(), now(), 'task_run_updated', '{}', now())"
    )
    library = MediaLibrary.create(
        name="wipe-lib", backend="local", backend_config={"root_path": "/library"}
    )
    clean_db.execute_sql(
        "INSERT INTO import_job (created_at, updated_at, source_path, library_id, task_run_id,"
        " state, transfer_mode, imported_count, skipped_count, failed_count, failed_files)"
        " VALUES (now(), now(), '/downloads/x', %s, %s, 'completed', 'copy', 1, 0, 0, '[]')",
        (library.id, run_id),
    )

    run_pending_migrations(clean_db)

    assert clean_db.execute_sql("SELECT count(*) FROM background_task_run").fetchone()[0] == 0
    assert clean_db.execute_sql("SELECT count(*) FROM system_event").fetchone()[0] == 0
    assert clean_db.execute_sql("SELECT task_run_id FROM import_job").fetchone()[0] is None
    assert "20260729_02_wipe_task_run_history" in _schema_migration_names(clean_db)


def test_run_pending_migrations_resets_interaction_sync_states_preserving_memory(clean_db):
    """互动同步迁 kernel（20260729_05）：保留 last_succeeded_at 记忆、清历史；未成功行删除。

    该时间戳是分层调度的唯一记忆载体，清空等于重放全库 seed（每片一次 JavDB 请求）。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260729_05_reset_movie_interaction_sync_states",),
    )
    clean_db.execute_sql(
        "INSERT INTO resource_task_state"
        " (created_at, updated_at, task_key, resource_type, resource_id, state,"
        "  attempt_count, retry_round, last_succeeded_at, last_error, error_code)"
        " VALUES"
        " (now(), now(), 'movie_interaction_sync', 'movie', 1, 'failed', 3, 0,"
        "  '2026-07-01 00:00:00', 'javdb 超时', 'javdb_request_error'),"
        " (now(), now(), 'movie_interaction_sync', 'movie', 2, 'running', 1, 0,"
        "  NULL, NULL, NULL)"
    )

    run_pending_migrations(clean_db)

    rows = clean_db.execute_sql(
        "SELECT resource_id, state, attempt_count, last_succeeded_at, last_error, error_code"
        " FROM resource_task_state WHERE task_key = 'movie_interaction_sync'"
        " ORDER BY resource_id"
    ).fetchall()
    assert len(rows) == 1
    resource_id, state, attempt_count, last_succeeded_at, last_error, error_code = rows[0]
    assert resource_id == 1
    assert state == "succeeded"
    assert attempt_count == 0
    assert last_succeeded_at is not None
    assert last_error is None
    assert error_code is None


def test_run_pending_migrations_wipes_cloud115_download_import_history(clean_db):
    """任务架构切换（20260730_02）：清空导入作业台账 + cloud115 下载任务，qB 行原样保留。

    qB 的行删不得——DownloadSyncService.sync_client 是 get_or_create，种子还在就会重建，
    且 import_status 复位成 pending 触发重新导入。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260730_02_wipe_cloud115_download_import_history",),
    )
    library = MediaLibrary.create(
        name="wipe-dl-lib", backend="local", backend_config={"root_path": "/library"}
    )
    clean_db.execute_sql(
        "INSERT INTO download_client (created_at, updated_at, name, kind, media_library_id)"
        " VALUES (NOW(), NOW(), 'qb-keep', 'qbittorrent', %s),"
        "        (NOW(), NOW(), 'cloud-drop', 'cloud115', %s)",
        (library.id, library.id),
    )
    qb_id, cloud_id = (
        row[0]
        for row in clean_db.execute_sql(
            "SELECT id FROM download_client ORDER BY id"
        ).fetchall()
    )
    clean_db.execute_sql(
        "INSERT INTO download_task (created_at, updated_at, client_id, name, info_hash,"
        " save_path, progress, download_state, import_status)"
        " VALUES (NOW(), NOW(), %s, 'QB-001', 'hash-qb', '/downloads/QB-001', 1.0,"
        "         'completed', 'completed'),"
        "        (NOW(), NOW(), %s, 'CL-001', 'hash-cl', '/downloads/CL-001', 1.0,"
        "         'completed', 'failed')",
        (qb_id, cloud_id),
    )
    task_id = clean_db.execute_sql(
        "SELECT id FROM download_task WHERE client_id = %s", (cloud_id,)
    ).fetchone()[0]
    clean_db.execute_sql(
        "INSERT INTO import_job (created_at, updated_at, source_path, library_id,"
        " download_task_id, state, transfer_mode, imported_count, skipped_count,"
        " failed_count, failed_files)"
        " VALUES (NOW(), NOW(), '/downloads/CL-001', %s, %s, 'failed', 'cleanup-source',"
        "         0, 0, 1, '[]')",
        (library.id, task_id),
    )
    VideoImportJob.create(
        source_path="/downloads/video",
        library=library,
        state="completed",
        transfer_mode="copy",
    )

    run_pending_migrations(clean_db)

    assert clean_db.execute_sql("SELECT count(*) FROM import_job").fetchone()[0] == 0
    assert clean_db.execute_sql("SELECT count(*) FROM video_import_job").fetchone()[0] == 0
    remaining = clean_db.execute_sql(
        "SELECT client_id FROM download_task"
    ).fetchall()
    assert remaining == [(qb_id,)]
    assert (
        "20260730_02_wipe_cloud115_download_import_history"
        in _schema_migration_names(clean_db)
    )


def test_run_pending_migrations_drops_movie_subtitle_fetch_states(clean_db):
    """20260730_03：删除孤儿 task_key movie_subtitle_fetch，其余 task_key 不受影响。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260730_03_drop_movie_subtitle_fetch_states",),
    )
    clean_db.execute_sql(
        "INSERT INTO resource_task_state"
        " (created_at, updated_at, task_key, resource_type, resource_id, state,"
        "  attempt_count, retry_round)"
        " VALUES"
        " (now(), now(), 'movie_subtitle_fetch', 'movie', 1, 'pending', 0, 0),"
        " (now(), now(), 'movie_subtitle_fetch', 'movie', 2, 'failed', 3, 0),"
        " (now(), now(), 'media_thumbnail_generation', 'media', 1, 'succeeded', 1, 0)"
    )

    run_pending_migrations(clean_db)

    rows = clean_db.execute_sql(
        "SELECT task_key FROM resource_task_state ORDER BY task_key"
    ).fetchall()
    assert rows == [("media_thumbnail_generation",)]
    assert "20260730_03_drop_movie_subtitle_fetch_states" in _schema_migration_names(clean_db)


def test_run_pending_migrations_wipes_rapid_upload_history(clean_db):
    """20260730_04：秒传批次与明细整体清空。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260730_04_wipe_rapid_upload_history",),
    )
    library = MediaLibrary.create(
        name="rapid-lib", backend="cloud115", backend_config={"root_cid": "1"}
    )
    clean_db.execute_sql(
        "INSERT INTO media_rapid_upload_batch (created_at, updated_at, target_library_id,"
        " state, total_count, succeeded_count, failed_count, cleanup_failed_count)"
        " VALUES (NOW(), NOW(), %s, 'completed', 1, 1, 0, 0)",
        (library.id,),
    )
    batch_id = clean_db.execute_sql("SELECT id FROM media_rapid_upload_batch").fetchone()[0]
    clean_db.execute_sql(
        "INSERT INTO media_rapid_upload_item (created_at, updated_at, batch_id, action,"
        " state, source_path, source_size_bytes, source_mtime_ns)"
        " VALUES (NOW(), NOW(), %s, 'rapid_upload', 'succeeded', '/mnt/a.mp4', 1, 1)",
        (batch_id,),
    )

    run_pending_migrations(clean_db)

    assert clean_db.execute_sql("SELECT count(*) FROM media_rapid_upload_item").fetchone()[0] == 0
    assert clean_db.execute_sql("SELECT count(*) FROM media_rapid_upload_batch").fetchone()[0] == 0
    assert "20260730_04_wipe_rapid_upload_history" in _schema_migration_names(clean_db)


def test_run_pending_migrations_adds_resource_task_attempt_finished_at_index(clean_db):
    """20260731_01：老库补 resource_task_attempt(finished_at) 单列索引；幂等地跳过已存在索引。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟老库：抹掉 initdb 建出的 (finished_at,) 单列索引并清 SchemaMigration 记录让 runner 重跑。
    # 索引名由 peewee 生成，不同版本可能不同，用列匹配定位后按实际名 DROP。
    stale_indexes = [
        index.name
        for index in clean_db.get_indexes("resource_task_attempt")
        if tuple(index.columns) == ("finished_at",)
    ]
    for index_name in stale_indexes:
        clean_db.execute_sql(f'DROP INDEX IF EXISTS "{index_name}"')
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.delete().where(
            SchemaMigration.name
            == "20260731_01_add_resource_task_attempt_finished_at_index"
        ).execute()

    run_pending_migrations(clean_db)

    indexed_columns = {
        tuple(index.columns)
        for index in clean_db.get_indexes("resource_task_attempt")
    }
    assert ("finished_at",) in indexed_columns
    assert (
        "20260731_01_add_resource_task_attempt_finished_at_index"
        in _schema_migration_names(clean_db)
    )

    # 幂等：再跑一次不应重复建索引出错。
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.delete().where(
            SchemaMigration.name
            == "20260731_01_add_resource_task_attempt_finished_at_index"
        ).execute()
    run_pending_migrations(clean_db)


def test_run_pending_migrations_wipes_system_notifications(clean_db):
    """20260730_05：通知历史整体清空。"""
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    run_pending_migrations(clean_db)
    clean_db.execute_sql(
        "DELETE FROM schema_migration WHERE name = %s",
        ("20260730_05_wipe_system_notifications",),
    )
    clean_db.execute_sql(
        "INSERT INTO system_notification (created_at, updated_at, category, title,"
        " content, is_read)"
        " VALUES (NOW(), NOW(), 'task', '导入完成', '正文', false)"
    )

    run_pending_migrations(clean_db)

    assert clean_db.execute_sql("SELECT count(*) FROM system_notification").fetchone()[0] == 0
    assert "20260730_05_wipe_system_notifications" in _schema_migration_names(clean_db)
