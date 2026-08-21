from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.model import (
    DownloadClient,
    DownloadTask,
    Media,
    MediaLibrary,
    Movie,
    SchemaMigration,
    SystemNotification,
)
from src.start.commands import main
from src.start.migrations.runner import (
    CONSOLIDATED_MIGRATION_NAME,
    SUPPORTED_BASE_MIGRATION_NAME,
    MigrationExecution,
    MigrationRunSummary,
    _list_migration_modules,
    _load_migration_module,
    run_pending_migrations,
)
from tests.conftest import TEST_MODELS


def _schema_migration_names(database) -> list[str]:
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return [
            item.name
            for item in SchemaMigration.select().order_by(SchemaMigration.id)
        ]


def _column_names(database, table_name: str) -> set[str]:
    return {column.name for column in database.get_columns(table_name)}


def _drop_columns(database, table_name: str, column_names: tuple[str, ...]) -> None:
    for column_name in column_names:
        database.execute_sql(
            f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}" CASCADE'
        )


def _create_legacy_task_tables(database) -> None:
    # 只构造 v0.4.21 收敛迁移需要读取的列，避免测试重新依赖已删除的旧模型。
    database.execute_sql(
        """
        CREATE TABLE resource_task_attempt (
            id SERIAL PRIMARY KEY
        )
        """
    )
    database.execute_sql(
        """
        CREATE TABLE resource_task_state (
            id SERIAL PRIMARY KEY,
            task_key VARCHAR(64) NOT NULL,
            resource_type VARCHAR(32) NOT NULL,
            resource_id INTEGER NOT NULL,
            state VARCHAR(32) NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            retry_round INTEGER NOT NULL DEFAULT 0,
            last_attempted_at TIMESTAMP NULL,
            last_succeeded_at TIMESTAMP NULL,
            next_retry_at TIMESTAMP NULL,
            error_code VARCHAR(64) NULL,
            last_error TEXT NULL,
            last_error_at TIMESTAMP NULL
        )
        """
    )


def test_only_v050_consolidated_migration_is_discoverable():
    modules = _list_migration_modules()

    assert [module.name for module in modules] == [CONSOLIDATED_MIGRATION_NAME]


def test_run_pending_migrations_accepts_v0421_base(clean_db):
    clean_db.create_tables([SchemaMigration])
    SchemaMigration.create(name=SUPPORTED_BASE_MIGRATION_NAME)

    summary = run_pending_migrations(clean_db)

    assert summary.executed == [
        MigrationExecution(name=CONSOLIDATED_MIGRATION_NAME, applied=True)
    ]
    assert CONSOLIDATED_MIGRATION_NAME in _schema_migration_names(clean_db)


def test_run_pending_migrations_rejects_unsupported_legacy_schema(clean_db):
    clean_db.execute_sql(
        "CREATE TABLE legacy_movie (id SERIAL PRIMARY KEY, title TEXT NOT NULL)"
    )

    with pytest.raises(ValueError, match="unsupported_migration_source"):
        run_pending_migrations(clean_db)

    assert CONSOLIDATED_MIGRATION_NAME not in _schema_migration_names(clean_db)


def test_run_pending_migrations_supports_fresh_database_and_is_idempotent(clean_db):
    first_summary = run_pending_migrations(clean_db)
    second_summary = run_pending_migrations(clean_db)

    assert first_summary.applied_count == 1
    assert second_summary.applied_count == 0
    assert _schema_migration_names(clean_db) == [CONSOLIDATED_MIGRATION_NAME]


def test_run_pending_migrations_rejects_current_schema_without_base_marker(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    with pytest.raises(ValueError, match="unsupported_migration_source"):
        run_pending_migrations(clean_db)


def test_consolidated_migration_upgrades_v0421_schema_and_preserves_required_memory(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    library = MediaLibrary.create(
        name="migration-library",
        backend="local",
        backend_config={"root_path": "/library"},
    )
    movie = Movie.create(movie_number="ABP-001", javdb_id="movie-1", title="title")
    media = Media.create(
        movie=movie,
        library=library,
        path="/library/ABP-001.mp4",
        content_fingerprint="media-fingerprint",
    )
    client = DownloadClient.create(name="migration-client", media_library=library)
    download_task = DownloadTask.create(
        client=client,
        movie="ABP-001",
        name="ABP-001",
        info_hash="migration-hash",
        save_path="/downloads/ABP-001",
        download_state="completed",
        import_status="running",
    )
    SystemNotification.create(
        category="info",
        title="历史通知",
        content="保留历史内容",
    )
    SchemaMigration.create(name=SUPPORTED_BASE_MIGRATION_NAME)

    _drop_columns(
        clean_db,
        "system_notification",
        ("event_type", "dedupe_key", "resource_type", "resource_id"),
    )
    _drop_columns(
        clean_db,
        "download_task",
        (
            "raw_state",
            "download_speed_bytes",
            "uploaded_speed_bytes",
            "downloaded_bytes",
            "total_size_bytes",
            "eta_seconds",
            "progress_synced_at",
            "import_task_run_id",
        ),
    )
    _drop_columns(
        clean_db,
        "movie",
        (
            "interaction_synced_at",
            "subscription_search_state",
            "subscription_search_attempt_count",
            "subscription_search_retry_round",
            "subscription_search_last_attempted_at",
            "subscription_search_last_succeeded_at",
            "subscription_search_next_retry_at",
            "subscription_search_error_code",
            "subscription_search_last_error",
            "subscription_search_last_error_at",
        ),
    )
    _drop_columns(
        clean_db,
        "media",
        (
            "thumbnail_generation_state",
            "thumbnail_attempt_count",
            "thumbnail_deferred_count",
            "thumbnail_next_retry_at",
            "thumbnail_last_error_code",
            "thumbnail_last_error",
            "thumbnail_terminal_at",
            "thumbnail_source_fingerprint",
        ),
    )
    _create_legacy_task_tables(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO resource_task_state (
            task_key, resource_type, resource_id, state, attempt_count,
            last_succeeded_at
        ) VALUES (
            'movie_interaction_sync', 'movie', %s, 'succeeded', 1,
            '2026-08-20 01:02:03'
        )
        """,
        (movie.id,),
    )
    clean_db.execute_sql(
        """
        INSERT INTO resource_task_state (
            task_key, resource_type, resource_id, state, attempt_count,
            next_retry_at, error_code, last_error, last_error_at
        ) VALUES (
            'media_thumbnail_generation', 'media', %s, 'failed_retryable', 2,
            '2026-08-21 01:02:03', 'decoder_error', 'temporary decoder error',
            '2026-08-20 01:02:03'
        )
        """,
        (media.id,),
    )
    clean_db.execute_sql(
        """
        INSERT INTO resource_task_state (
            task_key, resource_type, resource_id, state, attempt_count, retry_round,
            last_attempted_at, error_code, last_error, last_error_at
        ) VALUES (
            'subscribed_movie_auto_download', 'movie', %s, 'exhausted', 3, 2,
            '2026-08-22 01:02:03', 'no_candidate_found', '没有可用资源',
            '2026-08-22 01:02:03'
        )
        """,
        (movie.id,),
    )

    summary = run_pending_migrations(clean_db)

    assert summary.applied_count == 1
    assert clean_db.execute_sql(
        "SELECT interaction_synced_at FROM movie WHERE id = %s", (movie.id,)
    ).fetchone()[0] == datetime(2026, 8, 20, 1, 2, 3)
    media_state = clean_db.execute_sql(
        """
        SELECT thumbnail_generation_state, thumbnail_attempt_count,
               thumbnail_next_retry_at, thumbnail_last_error_code,
               thumbnail_source_fingerprint
        FROM media WHERE id = %s
        """,
        (media.id,),
    ).fetchone()
    assert media_state == (
        "retry_wait",
        2,
        datetime(2026, 8, 21, 1, 2, 3),
        "decoder_error",
        "media-fingerprint",
    )
    subscription_state = clean_db.execute_sql(
        """
        SELECT subscription_search_state, subscription_search_attempt_count,
               subscription_search_retry_round, subscription_search_error_code,
               subscription_search_last_error
        FROM movie WHERE id = %s
        """,
        (movie.id,),
    ).fetchone()
    assert subscription_state == (
        "exhausted",
        3,
        2,
        "no_candidate_found",
        "没有可用资源",
    )
    assert clean_db.execute_sql(
        """
        SELECT raw_state, download_speed_bytes, uploaded_speed_bytes,
               downloaded_bytes, total_size_bytes, import_status
        FROM download_task WHERE id = %s
        """,
        (download_task.id,),
    ).fetchone() == ("", 0, 0, 0, 0, "pending")
    assert {
        "event_type",
        "dedupe_key",
        "resource_type",
        "resource_id",
    } <= _column_names(clean_db, "system_notification")
    assert not clean_db.table_exists("resource_task_state")
    assert not clean_db.table_exists("resource_task_attempt")
    assert not clean_db.table_exists("system_event")
    assert not clean_db.table_exists("subtitle_import_job")
    assert not clean_db.table_exists("video_import_job")
    assert not clean_db.table_exists("import_job")


def test_load_migration_module_uses_package_import():
    module = _load_migration_module(
        Path("src/start/migrations/versions/20260821_01_consolidate_task_runtime.py")
    )

    assert module.name == CONSOLIDATED_MIGRATION_NAME
    assert callable(module.migrate)


def test_migrate_command_runs_the_consolidated_migration(monkeypatch):
    events = []
    legacy_database = object()
    ready_database = object()

    def fake_run_pending_migrations(database):
        events.append(database)
        return MigrationRunSummary(
            executed=[
                MigrationExecution(
                    name=CONSOLIDATED_MIGRATION_NAME,
                    applied=database is legacy_database,
                )
            ]
        )

    monkeypatch.setattr(
        "src.start.commands._connect_database_for_migration",
        lambda: legacy_database,
    )
    monkeypatch.setattr(
        "src.start.commands._ensure_database_ready",
        lambda: ready_database,
    )
    monkeypatch.setattr(
        "src.start.migrations.run_pending_migrations",
        fake_run_pending_migrations,
    )

    result = CliRunner().invoke(main, ["migrate"])

    assert result.exit_code == 0, result.output
    assert f"applied: {CONSOLIDATED_MIGRATION_NAME}" in result.output
    assert "migrate finished: applied=1 skipped=0 total=1" in result.output
    assert events == [legacy_database, ready_database]
