from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.model import (
    Actor,
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    Media,
    MediaLibrary,
    Movie,
    Playlist,
    PlaylistMovie,
    SchemaMigration,
    SystemNotification,
)
from src.start.commands import main
from src.start.legacy_v053_upgrade import LEGACY_V053_UPGRADE_MIGRATION_NAME
from src.start.migrations.runner import (
    ACTOR_GENDER_BACKFILL_MIGRATION_NAME,
    CONSOLIDATED_MIGRATION_NAME,
    IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
    MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
    MOVIE_BLACKLIST_MIGRATION_NAME,
    MOVIE_COLLECTION_OWNER_MIGRATION_NAME,
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


def test_current_migrations_are_discoverable_in_order():
    modules = _list_migration_modules()

    assert [module.name for module in modules] == [
        CONSOLIDATED_MIGRATION_NAME,
        MOVIE_COLLECTION_OWNER_MIGRATION_NAME,
        ACTOR_GENDER_BACKFILL_MIGRATION_NAME,
        MOVIE_BLACKLIST_MIGRATION_NAME,
        LEGACY_V053_UPGRADE_MIGRATION_NAME,
        MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
        IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
    ]


def test_run_pending_migrations_rejects_v0421_base(clean_db):
    clean_db.create_tables([SchemaMigration])
    SchemaMigration.create(name=SUPPORTED_BASE_MIGRATION_NAME)

    with pytest.raises(ValueError, match="unsupported_migration_source"):
        run_pending_migrations(clean_db)

    assert CONSOLIDATED_MIGRATION_NAME not in _schema_migration_names(clean_db)


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


def test_run_pending_migrations_completes_fresh_current_schema_after_model_creation(clean_db):
    first_summary = run_pending_migrations(clean_db)
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    second_summary = run_pending_migrations(clean_db)

    assert first_summary.applied_count == 1
    assert second_summary.executed == [
        MigrationExecution(name=CONSOLIDATED_MIGRATION_NAME, applied=False),
        MigrationExecution(name=MOVIE_COLLECTION_OWNER_MIGRATION_NAME, applied=True),
        MigrationExecution(name=ACTOR_GENDER_BACKFILL_MIGRATION_NAME, applied=True),
        MigrationExecution(name=MOVIE_BLACKLIST_MIGRATION_NAME, applied=True),
        MigrationExecution(name=LEGACY_V053_UPGRADE_MIGRATION_NAME, applied=True),
        MigrationExecution(name=MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME, applied=True),
        MigrationExecution(name=IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME, applied=True),
    ]
    assert _schema_migration_names(clean_db) == [
        CONSOLIDATED_MIGRATION_NAME,
        MOVIE_COLLECTION_OWNER_MIGRATION_NAME,
        ACTOR_GENDER_BACKFILL_MIGRATION_NAME,
        MOVIE_BLACKLIST_MIGRATION_NAME,
        LEGACY_V053_UPGRADE_MIGRATION_NAME,
        MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
        IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
    ]


def test_run_pending_migrations_rejects_current_schema_without_base_marker(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    with pytest.raises(ValueError, match="unsupported_migration_source"):
        run_pending_migrations(clean_db)


def test_consolidated_migration_upgrades_v0421_schema_and_preserves_required_memory(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql(
        "ALTER TABLE movie ADD COLUMN is_collection_overridden BOOLEAN NOT NULL DEFAULT FALSE"
    )
    clean_db.execute_sql(
        "ALTER TABLE background_task_run ADD COLUMN owner_pid INTEGER NULL"
    )
    library = MediaLibrary.create(
        name="migration-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(movie_number="ABP-001", javdb_id="movie-1", title="title")
    clean_db.execute_sql(
        "UPDATE movie SET is_collection_overridden = TRUE WHERE id = %s",
        (movie.id,),
    )
    media = Media.create(
        movie=movie,
        library=library,
        file_name="ABP-001.mp4",
    )
    client = DownloadClient.create(
        name="migration-client",
        library=library,
        provider_config={},
    )
    download_task = DownloadTask.create(
        client=client,
        movie="ABP-001",
        name="ABP-001",
        remote_id="migration-hash",
        state="completed",
        completed_source_ref={"source": "ABP-001"},
        import_status="running",
    )
    SystemNotification.create(
        category="info",
        title="历史通知",
        content="保留历史内容",
    )
    active_run = BackgroundTaskRun.create(
        task_key="legacy_task",
        task_name="legacy task",
        trigger_type="manual",
        mutex_key="aps:legacy_task",
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
        ("import_task_run_id",),
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

    consolidated = _load_migration_module(
        Path(
            "src/start/migrations/versions/"
            "20260821_01_consolidate_task_runtime.py"
        )
    )
    with clean_db.atomic():
        consolidated.migrate(clean_db)
        SchemaMigration.create(name=CONSOLIDATED_MIGRATION_NAME)
    summary = run_pending_migrations(clean_db)

    assert summary.applied_count == 5
    assert clean_db.execute_sql(
        "SELECT interaction_synced_at FROM movie WHERE id = %s", (movie.id,)
    ).fetchone()[0] == datetime(2026, 8, 20, 1, 2, 3)
    media_state = clean_db.execute_sql(
        """
        SELECT thumbnail_generation_state, thumbnail_attempt_count,
               thumbnail_next_retry_at, thumbnail_last_error_code
        FROM media WHERE id = %s
        """,
        (media.id,),
    ).fetchone()
    assert media_state == (
        "retry_wait",
        2,
        datetime(2026, 8, 21, 1, 2, 3),
        "decoder_error",
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
    assert SystemNotification.select().count() == 0
    migrated_run = BackgroundTaskRun.get_by_id(active_run.id)
    assert migrated_run.state == "failed"
    assert migrated_run.mutex_key is None
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
    assert "is_collection_overridden" not in _column_names(clean_db, "movie")
    assert clean_db.execute_sql(
        "SELECT field_owners FROM movie WHERE id = %s", (movie.id,)
    ).fetchone()[0] == {"is_collection": "host:manual"}
    assert "is_blacklisted" in _column_names(clean_db, "movie")


def test_remove_media_special_tags_migration_drops_data_and_virtual_playlists(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql(
        "ALTER TABLE media ADD COLUMN special_tags VARCHAR(255) NOT NULL DEFAULT '普通'"
    )
    movie = Movie.create(movie_number="TAG-001", javdb_id="tag-movie", title="tag movie")
    recently_played = Playlist.create(kind="recently_played", name="最近播放")
    custom_playlist = Playlist.create(kind="custom", name="自定义列表")
    virtual_playlist = Playlist.create(kind="vr", name="VR")
    Playlist.create(kind="4k", name="4K")
    PlaylistMovie.create(playlist=recently_played, movie=movie)
    PlaylistMovie.create(playlist=custom_playlist, movie=movie)
    PlaylistMovie.create(playlist=virtual_playlist, movie=movie)

    migration = _load_migration_module(
        Path(
            "src/start/migrations/versions/"
            "20260826_01_remove_media_special_tags.py"
        )
    )
    migration.migrate(clean_db)

    assert "special_tags" not in _column_names(clean_db, "media")
    assert [playlist.kind for playlist in Playlist.select().order_by(Playlist.id)] == [
        recently_played.kind,
        custom_playlist.kind,
    ]
    assert {
        (item.playlist.kind, item.movie_id)
        for item in PlaylistMovie.select().order_by(PlaylistMovie.playlist, PlaylistMovie.movie)
    } == {
        (recently_played.kind, movie.id),
        (custom_playlist.kind, movie.id),
    }


def test_actor_gender_backfill_migration_handles_old_and_new_movie_extra_shapes(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    female = Actor.create(javdb_id="actor-female", name="female", gender=0)
    male = Actor.create(javdb_id="actor-male", name="male", gender=1)
    unknown = Actor.create(javdb_id="actor-unknown", name="unknown", gender=1)
    untouched = Actor.create(javdb_id="actor-untouched", name="untouched", gender=2)

    Movie.create(
        movie_number="OLD-001",
        javdb_id="old-movie",
        title="old",
        extra={
            "data": {
                "movie": {
                    "actors": [
                        {"id": female.javdb_id, "gender": 1},
                        {"id": male.javdb_id, "gender": 1},
                    ]
                }
            }
        },
    )
    Movie.create(
        movie_number="NEW-001",
        javdb_id="new-movie",
        title="new",
        extra={
            "data": {
                "movie": {
                    "actors": [
                        # 最新影片的值胜出，验证同一演员去重后的确定性。
                        {"id": female.javdb_id, "gender": 0},
                        {"id": unknown.javdb_id},
                        {"id": "actor-missing", "gender": 0},
                    ]
                }
            }
        },
    )
    Movie.create(
        movie_number="EMPTY-001",
        javdb_id="empty-movie",
        title="empty",
        extra={"data": {"movie": {"actors": "legacy-scalar"}}},
    )
    Movie.create(
        movie_number="NULL-001",
        javdb_id="null-movie",
        title="null",
        extra=None,
    )

    migration = _load_migration_module(
        Path(
            "src/start/migrations/versions/"
            "20260823_02_backfill_actor_gender_from_movie_extra.py"
        )
    )
    migration.migrate(clean_db)

    assert Actor.get_by_id(female.id).gender == 1
    assert Actor.get_by_id(male.id).gender == 2
    assert Actor.get_by_id(unknown.id).gender == 1
    assert Actor.get_by_id(untouched.id).gender == 2


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
