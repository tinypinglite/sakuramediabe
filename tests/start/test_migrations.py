import json
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.model import (
    Actor,
    BackgroundTaskRun,
    DownloadClient,
    DownloadSubmissionRecord,
    DownloadTask,
    Image,
    Media,
    MediaLibrary,
    Movie,
    Playlist,
    PlaylistMovie,
    SchemaMigration,
    SystemNotification,
)
from src.start.commands import main
from src.start.migrations.runner import (
    ACTOR_GENDER_BACKFILL_MIGRATION_NAME,
    ACTOR_LOCAL_PROFILE_MIGRATION_NAME,
    ACTOR_MERGED_INTO_MIGRATION_NAME,
    ACTOR_METADATA_MIGRATION_NAME,
    API_KEYS_MIGRATION_NAME,
    CONSOLIDATED_MIGRATION_NAME,
    DETACH_USER_ASSET_IMAGES_MIGRATION_NAME,
    DOWNLOAD_RESOURCE_HISTORY_MIGRATION_NAME,
    DOWNLOAD_SUBMISSION_INDEXES_MIGRATION_NAME,
    DOWNLOAD_TASK_REMOTE_SEEN_MIGRATION_NAME,
    DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME,
    DROP_MOVIE_EXTRA_MIGRATION_NAME,
    HOT_REVIEW_ITEM_REMOVAL_MIGRATION_NAME,
    IMAGE_ORIGIN_PATTERN_INDEX_MIGRATION_NAME,
    IMAGE_SEARCH_INDEX_SPACE_STATE_MIGRATION_NAME,
    IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
    MEDIA_IMPORT_SOURCE_IDENTITY_MIGRATION_NAME,
    MEDIA_POINT_PRESERVATION_MIGRATION_NAME,
    MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
    MOMENT_COLLECTIONS_MIGRATION_NAME,
    MOVIE_BLACKLIST_MIGRATION_NAME,
    MOVIE_COLLECTION_OWNER_MIGRATION_NAME,
    PLUGIN_COLLECTION_OWNERSHIP_MIGRATION_NAME,
    PLUGIN_MOVIE_METADATA_MIGRATION_NAME,
    REMOVE_GENERATED_THIN_COVER_MIGRATION_NAME,
    REMOVE_ORPHAN_VIDEO_ITEMS_MIGRATION_NAME,
    WIDEN_DOWNLOAD_TITLE_COLUMNS_MIGRATION_NAME,
    MigrationExecution,
    MigrationRunSummary,
    _list_migration_modules,
    _load_migration_module,
    run_pending_migrations,
)
from tests.conftest import TEST_MODELS

V0421_BASE_MIGRATION_NAME = "20260816_01_add_movie_field_owners"


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
        MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
        IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
        IMAGE_SEARCH_INDEX_SPACE_STATE_MIGRATION_NAME,
        HOT_REVIEW_ITEM_REMOVAL_MIGRATION_NAME,
        MEDIA_IMPORT_SOURCE_IDENTITY_MIGRATION_NAME,
        ACTOR_METADATA_MIGRATION_NAME,
        PLUGIN_MOVIE_METADATA_MIGRATION_NAME,
        DOWNLOAD_RESOURCE_HISTORY_MIGRATION_NAME,
        MOMENT_COLLECTIONS_MIGRATION_NAME,
        ACTOR_LOCAL_PROFILE_MIGRATION_NAME,
        PLUGIN_COLLECTION_OWNERSHIP_MIGRATION_NAME,
        MEDIA_POINT_PRESERVATION_MIGRATION_NAME,
        DROP_MOVIE_EXTRA_MIGRATION_NAME,
        REMOVE_ORPHAN_VIDEO_ITEMS_MIGRATION_NAME,
        ACTOR_MERGED_INTO_MIGRATION_NAME,
        DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME,
        IMAGE_ORIGIN_PATTERN_INDEX_MIGRATION_NAME,
        REMOVE_GENERATED_THIN_COVER_MIGRATION_NAME,
        WIDEN_DOWNLOAD_TITLE_COLUMNS_MIGRATION_NAME,
        DOWNLOAD_TASK_REMOTE_SEEN_MIGRATION_NAME,
        DOWNLOAD_SUBMISSION_INDEXES_MIGRATION_NAME,
        API_KEYS_MIGRATION_NAME,
        DETACH_USER_ASSET_IMAGES_MIGRATION_NAME,
    ]


def test_plugin_metadata_migration_preserves_movies_and_allows_multiple_null_ids(clean_db):
    clean_db.create_tables(TEST_MODELS)
    _drop_columns(clean_db, "movie", ("metadata_source", "javdb_next_check_at"))
    clean_db.execute_sql("ALTER TABLE movie ALTER COLUMN javdb_id SET NOT NULL")
    existing = Movie.create(movie_number="OLD-001", javdb_id="real-id", title="Original")
    module = _load_migration_module(Path(f"{PLUGIN_MOVIE_METADATA_MIGRATION_NAME}.py"))
    module.migrate(clean_db)
    current = Movie.get_by_id(existing.id)
    assert current.javdb_id == "real-id" and current.title == "Original"
    assert current.metadata_source is None and current.javdb_next_check_at is None
    for number in ("NEW-001", "NEW-002"):
        Movie.create(movie_number=number, javdb_id=None, title="Plugin", metadata_source={"plugin_id": "test"})
    assert Movie.select().where(Movie.javdb_id.is_null()).count() == 2


def test_plugin_collection_ownership_migration_adds_columns_without_losing_rows(clean_db):
    clean_db.create_tables(TEST_MODELS)
    existing = Playlist.create(name="existing-plugin-migration")
    for table in ("playlist", "moment_collection", "clip_collection"):
        _drop_columns(clean_db, table, ("owner_plugin_id", "plugin_key"))

    migration = _load_migration_module(
        Path(f"{PLUGIN_COLLECTION_OWNERSHIP_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)

    for table in ("playlist", "moment_collection", "clip_collection"):
        assert {"owner_plugin_id", "plugin_key"} <= _column_names(clean_db, table)
    assert Playlist.get_by_id(existing.id).name == "existing-plugin-migration"


def test_run_pending_migrations_rejects_v0421_base(clean_db):
    clean_db.create_tables([SchemaMigration])
    SchemaMigration.create(name=V0421_BASE_MIGRATION_NAME)

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
        MigrationExecution(name=MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME, applied=True),
        MigrationExecution(name=IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME, applied=True),
        MigrationExecution(name=IMAGE_SEARCH_INDEX_SPACE_STATE_MIGRATION_NAME, applied=True),
        MigrationExecution(name=HOT_REVIEW_ITEM_REMOVAL_MIGRATION_NAME, applied=True),
        MigrationExecution(name=MEDIA_IMPORT_SOURCE_IDENTITY_MIGRATION_NAME, applied=True),
        MigrationExecution(name=ACTOR_METADATA_MIGRATION_NAME, applied=True),
        MigrationExecution(name=PLUGIN_MOVIE_METADATA_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DOWNLOAD_RESOURCE_HISTORY_MIGRATION_NAME, applied=True),
        MigrationExecution(name=MOMENT_COLLECTIONS_MIGRATION_NAME, applied=True),
        MigrationExecution(name=ACTOR_LOCAL_PROFILE_MIGRATION_NAME, applied=True),
        MigrationExecution(name=PLUGIN_COLLECTION_OWNERSHIP_MIGRATION_NAME, applied=True),
        MigrationExecution(name=MEDIA_POINT_PRESERVATION_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DROP_MOVIE_EXTRA_MIGRATION_NAME, applied=True),
        MigrationExecution(name=REMOVE_ORPHAN_VIDEO_ITEMS_MIGRATION_NAME, applied=True),
        MigrationExecution(name=ACTOR_MERGED_INTO_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME, applied=True),
        MigrationExecution(name=IMAGE_ORIGIN_PATTERN_INDEX_MIGRATION_NAME, applied=True),
        MigrationExecution(name=REMOVE_GENERATED_THIN_COVER_MIGRATION_NAME, applied=True),
        MigrationExecution(name=WIDEN_DOWNLOAD_TITLE_COLUMNS_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DOWNLOAD_TASK_REMOTE_SEEN_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DOWNLOAD_SUBMISSION_INDEXES_MIGRATION_NAME, applied=True),
        MigrationExecution(name=API_KEYS_MIGRATION_NAME, applied=True),
        MigrationExecution(name=DETACH_USER_ASSET_IMAGES_MIGRATION_NAME, applied=True),
    ]
    assert _schema_migration_names(clean_db) == [
        CONSOLIDATED_MIGRATION_NAME,
        MOVIE_COLLECTION_OWNER_MIGRATION_NAME,
        ACTOR_GENDER_BACKFILL_MIGRATION_NAME,
        MOVIE_BLACKLIST_MIGRATION_NAME,
        MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME,
        IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME,
        IMAGE_SEARCH_INDEX_SPACE_STATE_MIGRATION_NAME,
        HOT_REVIEW_ITEM_REMOVAL_MIGRATION_NAME,
        MEDIA_IMPORT_SOURCE_IDENTITY_MIGRATION_NAME,
        ACTOR_METADATA_MIGRATION_NAME,
        PLUGIN_MOVIE_METADATA_MIGRATION_NAME,
        DOWNLOAD_RESOURCE_HISTORY_MIGRATION_NAME,
        MOMENT_COLLECTIONS_MIGRATION_NAME,
        ACTOR_LOCAL_PROFILE_MIGRATION_NAME,
        PLUGIN_COLLECTION_OWNERSHIP_MIGRATION_NAME,
        MEDIA_POINT_PRESERVATION_MIGRATION_NAME,
        DROP_MOVIE_EXTRA_MIGRATION_NAME,
        REMOVE_ORPHAN_VIDEO_ITEMS_MIGRATION_NAME,
        ACTOR_MERGED_INTO_MIGRATION_NAME,
        DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME,
        IMAGE_ORIGIN_PATTERN_INDEX_MIGRATION_NAME,
        REMOVE_GENERATED_THIN_COVER_MIGRATION_NAME,
        WIDEN_DOWNLOAD_TITLE_COLUMNS_MIGRATION_NAME,
        DOWNLOAD_TASK_REMOTE_SEEN_MIGRATION_NAME,
        DOWNLOAD_SUBMISSION_INDEXES_MIGRATION_NAME,
        API_KEYS_MIGRATION_NAME,
        DETACH_USER_ASSET_IMAGES_MIGRATION_NAME,
    ]


def test_download_task_remote_seen_migration_backfills_existing_rows(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟加列之前的结构：删掉新列后写入一行存量任务。
    _drop_columns(clean_db, "download_task", ("remote_seen_at",))
    library = MediaLibrary.create(
        name="remote-seen-migration",
        provider_key="test",
        provider_config={},
    )
    client = DownloadClient.create(
        name="remote-seen-migration-client",
        library=library,
        provider_config={},
    )
    task = DownloadTask.create(
        client=client,
        remote_id="legacy",
        name="legacy",
        state="downloading",
        progress=0,
        import_status="pending",
    )

    migration = _load_migration_module(
        Path(f"{DOWNLOAD_TASK_REMOTE_SEEN_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)
    backfilled = DownloadTask.get_by_id(task.id)
    assert backfilled.remote_seen_at is not None
    assert "remote_seen_at" in _column_names(clean_db, "download_task")

    # 幂等：重复执行不报错，也不覆盖已有的可见时间。
    sentinel = datetime(2020, 1, 1)
    DownloadTask.update(remote_seen_at=sentinel).where(
        DownloadTask.id == task.id
    ).execute()
    migration.migrate(clean_db)
    assert DownloadTask.get_by_id(task.id).remote_seen_at == sentinel


def test_download_submission_indexes_migration_adds_query_indexes(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟旧结构：提交历史表只有 task_id 索引。
    for name in (
        "downloadsubmissionrecord_client_id_remote_id",
        "downloadsubmissionrecord_client_id_info_hash",
        "downloadsubmissionrecord_updated_at",
    ):
        clean_db.execute_sql(f'DROP INDEX IF EXISTS "{name}"')

    migration = _load_migration_module(
        Path(f"{DOWNLOAD_SUBMISSION_INDEXES_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)

    columns = {
        tuple(index.columns)
        for index in clean_db.get_indexes("download_submission_record")
    }
    assert ("client_id", "remote_id") in columns
    assert ("client_id", "info_hash") in columns
    assert ("updated_at",) in columns

    # 幂等：重复执行不报错，也不重复建索引。
    before = len(clean_db.get_indexes("download_submission_record"))
    migration.migrate(clean_db)
    assert len(clean_db.get_indexes("download_submission_record")) == before


def test_run_pending_migrations_rejects_current_schema_without_base_marker(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    with pytest.raises(ValueError, match="unsupported_migration_source"):
        run_pending_migrations(clean_db)


def test_widen_download_title_migration_allows_long_titles(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 模拟旧结构：两列仍是 varchar(255)，超过 255 字符的种子标题无法入库。
    clean_db.execute_sql(
        "ALTER TABLE download_submission_record ALTER COLUMN title TYPE VARCHAR(255)"
    )
    clean_db.execute_sql(
        "ALTER TABLE download_task ALTER COLUMN name TYPE VARCHAR(255)"
    )
    # 模拟用户已按 issue 临时方案自行把 title 改成 text，迁移应跳过该列。
    clean_db.execute_sql(
        "ALTER TABLE download_submission_record ALTER COLUMN title TYPE TEXT"
    )
    long_title = "T" * 321

    migration = _load_migration_module(
        Path(f"{WIDEN_DOWNLOAD_TITLE_COLUMNS_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)

    record = DownloadSubmissionRecord.create(
        client_id=1,
        movie_number="TEST-001",
        indexer_name="indexer",
        title=long_title,
        source_uri=f"magnet:?xt=urn:btih:{'0' * 40}",
        info_hash="0" * 40,
    )
    library = MediaLibrary.create(
        name="widen-download-title",
        provider_key="test",
        provider_config={},
    )
    client = DownloadClient.create(
        name="widen-download-title-client",
        library=library,
        provider_config={},
    )
    task = DownloadTask.create(
        client=client,
        movie="TEST-001",
        remote_id="widen-download-title-remote",
        name=long_title,
        state="queued",
        progress=0,
        import_status="pending",
    )
    assert DownloadSubmissionRecord.get_by_id(record.id).title == long_title
    assert DownloadTask.get_by_id(task.id).name == long_title

    # 用户已自行改成 text 的库：重跑不报错、数据保留。
    migration.migrate(clean_db)
    assert DownloadSubmissionRecord.get_by_id(record.id).title == long_title
    assert DownloadTask.get_by_id(task.id).name == long_title


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
    SchemaMigration.create(name=V0421_BASE_MIGRATION_NAME)

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

    assert summary.applied_count == 26
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


def test_remove_hot_review_item_migration_drops_snapshot_table(clean_db):
    clean_db.execute_sql("CREATE TABLE hot_review_item (id SERIAL PRIMARY KEY)")

    migration = _load_migration_module(
        Path("src/start/migrations/versions/20260831_02_remove_hot_review_item.py")
    )
    migration.migrate(clean_db)

    assert not clean_db.table_exists("hot_review_item")


def test_media_import_source_identity_migration_adds_column_and_index(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    _drop_columns(clean_db, "media", ("import_source_identity",))

    migration = _load_migration_module(
        Path(
            "src/start/migrations/versions/"
            "20260903_01_add_media_import_source_identity.py"
        )
    )
    migration.migrate(clean_db)

    assert "import_source_identity" in _column_names(clean_db, "media")
    assert "media_library_id_import_source_identity" in {
        index.name for index in clean_db.get_indexes("media")
    }


def test_image_origin_pattern_index_migration_creates_index(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql("DROP INDEX IF EXISTS image_origin_pattern")

    _load_migration_module(
        Path(f"{IMAGE_ORIGIN_PATTERN_INDEX_MIGRATION_NAME}.py")
    ).migrate(clean_db)

    assert "image_origin_pattern" in {
        index.name for index in clean_db.get_indexes("image")
    }


def test_local_profile_and_moment_collection_migrations_add_runtime_indexes(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql("DROP TABLE moment_collection_item")
    clean_db.execute_sql("DROP TABLE moment_collection")
    _drop_columns(
        clean_db,
        "actor",
        ("display_name_override", "profile_image_override_id"),
    )

    _load_migration_module(
        Path(f"{MOMENT_COLLECTIONS_MIGRATION_NAME}.py")
    ).migrate(clean_db)
    _load_migration_module(
        Path(f"{ACTOR_LOCAL_PROFILE_MIGRATION_NAME}.py")
    ).migrate(clean_db)

    assert "momentcollectionitem_point_id" in {
        index.name for index in clean_db.get_indexes("moment_collection_item")
    }
    assert "actor_profile_image_override_id" in {
        index.name for index in clean_db.get_indexes("actor")
    }


def test_api_keys_migration_creates_table(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql("DROP TABLE api_keys")

    _load_migration_module(Path(f"{API_KEYS_MIGRATION_NAME}.py")).migrate(clean_db)

    assert clean_db.table_exists("api_keys")
    assert {"name", "key_hint", "key_hash", "last_used_at"} <= _column_names(
        clean_db, "api_keys"
    )


def _set_movie_extra(database, movie_id: int, extra) -> None:
    database.execute_sql(
        "UPDATE movie SET extra = %s WHERE id = %s",
        (
            json.dumps(extra, ensure_ascii=False) if extra is not None else None,
            movie_id,
        ),
    )


def test_actor_gender_backfill_migration_handles_old_and_new_movie_extra_shapes(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    # 当前模型已移除 extra 列；临时重建旧列，覆盖历史迁移读取旧 schema 的路径。
    clean_db.execute_sql("ALTER TABLE movie ADD COLUMN IF NOT EXISTS extra TEXT")

    female = Actor.create(javdb_id="actor-female", name="female", gender=0)
    male = Actor.create(javdb_id="actor-male", name="male", gender=1)
    unknown = Actor.create(javdb_id="actor-unknown", name="unknown", gender=1)
    untouched = Actor.create(javdb_id="actor-untouched", name="untouched", gender=2)

    old_movie = Movie.create(movie_number="OLD-001", javdb_id="old-movie", title="old")
    _set_movie_extra(
        clean_db,
        old_movie.id,
        {
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
    new_movie = Movie.create(movie_number="NEW-001", javdb_id="new-movie", title="new")
    _set_movie_extra(
        clean_db,
        new_movie.id,
        {
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
    empty_movie = Movie.create(
        movie_number="EMPTY-001", javdb_id="empty-movie", title="empty"
    )
    _set_movie_extra(
        clean_db, empty_movie.id, {"data": {"movie": {"actors": "legacy-scalar"}}}
    )
    null_movie = Movie.create(movie_number="NULL-001", javdb_id="null-movie", title="null")
    _set_movie_extra(clean_db, null_movie.id, None)

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


def test_actor_gender_backfill_migration_skips_when_extra_column_absent(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    actor = Actor.create(javdb_id="actor-guard", name="guard", gender=1)

    migration = _load_migration_module(
        Path(
            "src/start/migrations/versions/"
            "20260823_02_backfill_actor_gender_from_movie_extra.py"
        )
    )
    migration.migrate(clean_db)

    assert Actor.get_by_id(actor.id).gender == 1


def test_drop_movie_extra_migration_removes_column(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    clean_db.execute_sql("ALTER TABLE movie ADD COLUMN IF NOT EXISTS extra TEXT")

    _load_migration_module(Path(f"{DROP_MOVIE_EXTRA_MIGRATION_NAME}.py")).migrate(
        clean_db
    )

    assert "extra" not in _column_names(clean_db, "movie")


def test_drop_image_derived_sizes_migration_removes_columns(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    for column_name in ("small", "medium", "large"):
        clean_db.execute_sql(
            f'ALTER TABLE image ADD COLUMN IF NOT EXISTS "{column_name}" VARCHAR(255) NOT NULL DEFAULT \'\''
        )

    _load_migration_module(Path(f"{DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME}.py")).migrate(
        clean_db
    )

    assert not {"small", "medium", "large"} & _column_names(clean_db, "image")


def test_migrate_command_runs_the_consolidated_migration(monkeypatch):
    events = []
    optional_service_calls = []
    maintenance_calls = []

    class FakeLegacyDatabase:
        def get_tables(self):
            return ["movie"]

    legacy_database = FakeLegacyDatabase()
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

    def fake_initialize_optional_services(*, existing_deployment):
        optional_service_calls.append(existing_deployment)

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
    monkeypatch.setattr(
        "src.config.config.initialize_optional_services",
        fake_initialize_optional_services,
    )
    monkeypatch.setattr(
        "src.start.maintenance.run_startup_maintenance",
        lambda database: maintenance_calls.append(database),
    )

    result = CliRunner().invoke(main, ["migrate"])

    assert result.exit_code == 0, result.output
    assert f"applied: {CONSOLIDATED_MIGRATION_NAME}" in result.output
    assert "migrate finished: applied=1 skipped=0 total=1" in result.output
    assert events == [legacy_database, ready_database]
    assert optional_service_calls == [True]
    assert maintenance_calls == [ready_database]


@pytest.mark.parametrize('kind', ['jav', 'video'])
def test_media_point_migration_preserves_old_points_and_collection_membership(clean_db, kind):
    from src.model import (
        Image,
        MediaPoint,
        MediaThumbnail,
        MomentCollection,
        MomentCollectionItem,
        VideoItem,
    )

    clean_db.create_tables(TEST_MODELS)
    library = MediaLibrary.create(name='migration', provider_key='demo', provider_config={})
    owner = (
        {'movie': Movie.create(movie_number='MIGRATE-001', title='JAV')}
        if kind == 'jav' else {'video_item': VideoItem.create(title='Video')}
    )
    media = Media.create(library=library, file_name='source.mp4', **owner)
    image = Image.create(origin='saved.webp')
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=42)
    # 用真正的旧字段和级联约束构造升级前的数据。
    _drop_columns(clean_db, 'media_point', ('image_id', 'movie_number', 'video_item_id'))
    clean_db.execute_sql('''
        ALTER TABLE media_point
            ALTER COLUMN media_id SET NOT NULL,
            ALTER COLUMN thumbnail_id SET NOT NULL,
            DROP CONSTRAINT media_point_media_id_fkey,
            DROP CONSTRAINT media_point_thumbnail_id_fkey,
            ADD CONSTRAINT media_point_media_id_fkey FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE CASCADE,
            ADD CONSTRAINT media_point_thumbnail_id_fkey FOREIGN KEY (thumbnail_id) REFERENCES media_thumbnail(id) ON DELETE CASCADE
    ''')
    created_at = datetime(2026, 1, 1, 12, 0)
    point_id = clean_db.execute_sql(
        'INSERT INTO media_point (media_id, thumbnail_id, offset_seconds, created_at, updated_at) '
        'VALUES (%s, %s, 42, %s, %s) RETURNING id',
        (media.id, thumbnail.id, created_at, created_at),
    ).fetchone()[0]
    collection = MomentCollection.create(name='old collection')
    member = MomentCollectionItem.create(collection=collection, point=point_id, position=3)

    migration = _load_migration_module(Path(f'{MEDIA_POINT_PRESERVATION_MIGRATION_NAME}.py'))
    migration.migrate(clean_db)
    point = MediaPoint.get_by_id(point_id)
    assert point.image_id == image.id
    assert point.movie_number == media.movie_number and point.video_item_id == media.video_item_id
    assert point.offset_seconds == 42 and point.created_at == created_at
    assert MomentCollectionItem.get_by_id(member.id).position == 3
    Media.delete().where(Media.id == media.id).execute()
    if kind == 'video':
        owner['video_item'].delete_instance(recursive=True)
    point = MediaPoint.get_by_id(point_id)
    assert point.media_id is None and point.thumbnail_id is None
    assert point.image_id == image.id
    assert point.movie_number == media.movie_number and point.video_item_id == media.video_item_id
    assert MomentCollectionItem.get_by_id(member.id).point_id == point_id
    # 当前模型建表后的重复迁移也不能清空已经独立的时刻快照。
    migration.migrate(clean_db)
    assert MediaPoint.get_by_id(point_id).image_id == image.id


def test_remove_orphan_video_items_migration_deletes_empty_videos_and_membership(clean_db):
    from src.model import (
        Image,
        MediaPoint,
        VideoCollection,
        VideoCollectionItem,
        VideoItem,
    )

    clean_db.create_tables(TEST_MODELS)
    library = MediaLibrary.create(name='orphan-cleanup', provider_key='demo', provider_config={})
    orphan = VideoItem.create(title='orphan video')
    kept = VideoItem.create(title='kept video')
    kept_media = Media.create(video_item=kept, library=library, file_name='kept.mp4')
    collection = VideoCollection.create(name='mixed collection')
    orphan_link = VideoCollectionItem.create(collection=collection, video_item=orphan, position=0)
    kept_link = VideoCollectionItem.create(collection=collection, video_item=kept, position=1)
    image = Image.create(origin='orphan.webp')
    point = MediaPoint.create(image=image, video_item_id=orphan.id, offset_seconds=10)

    migration = _load_migration_module(Path(f'{REMOVE_ORPHAN_VIDEO_ITEMS_MIGRATION_NAME}.py'))
    migration.migrate(clean_db)

    assert VideoItem.get_or_none(VideoItem.id == orphan.id) is None
    assert not VideoCollectionItem.select().where(VideoCollectionItem.id == orphan_link.id).exists()
    assert VideoItem.get_or_none(VideoItem.id == kept.id) is not None
    assert Media.get_or_none(Media.id == kept_media.id) is not None
    assert VideoCollectionItem.get_by_id(kept_link.id).video_item_id == kept.id
    # 时刻是无外键的展示快照，不随条目删除。
    assert MediaPoint.get_by_id(point.id).video_item_id == orphan.id
    # 重复执行幂等：有效条目与成员不受影响。
    migration.migrate(clean_db)
    assert VideoCollectionItem.get_by_id(kept_link.id).video_item_id == kept.id


def test_remove_generated_thin_cover_migration_clears_only_skipped_movies(clean_db, monkeypatch, tmp_path):
    import zipfile

    from src.common.media_paths import (
        movie_asset_relative_dir,
        normalize_asset_dir_name,
    )
    from src.config.config import settings

    clean_db.create_tables(TEST_MODELS)
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    def relative_dir(movie_number: str) -> str:
        return movie_asset_relative_dir(normalize_asset_dir_name(movie_number)).as_posix()

    def create_movie(movie_number: str, thin_name: str):
        directory = relative_dir(movie_number)
        cover = Image.create(origin=f"{directory}/cover.jpg")
        thin = Image.create(origin=f"{directory}/{thin_name}")
        movie = Movie.create(
            movie_number=movie_number,
            title=movie_number,
            cover_image=cover,
            thin_cover_image=thin,
        )
        return movie, thin

    def write_pack(movie_number: str, entry_names: list[str]) -> Path:
        directory = image_root / relative_dir(movie_number)
        directory.mkdir(parents=True, exist_ok=True)
        pack_path = directory / "assets.zip"
        with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
            for entry_name in entry_names:
                archive.writestr(entry_name, f"bytes:{entry_name}".encode())
        return pack_path

    def pack_entries(pack_path: Path) -> list[str]:
        with zipfile.ZipFile(pack_path) as archive:
            return archive.namelist()

    packed_movie, packed_thin = create_movie("FC2-4811064", "thin-cover.jpg")
    packed_path = write_pack("FC2-4811064", ["cover.jpg", "thin-cover.jpg"])
    loose_movie, loose_thin = create_movie("HEYZO-0733", "thin-cover.webp")
    loose_dir = image_root / relative_dir("HEYZO-0733")
    loose_dir.mkdir(parents=True, exist_ok=True)
    (loose_dir / "cover.webp").write_bytes(b"cover")
    (loose_dir / "thin-cover.webp").write_bytes(b"thin")
    western_movie, western_thin = create_movie("blacked.16.12.16", "thin-cover.jpg")
    western_path = write_pack("blacked.16.12.16", ["cover.jpg", "thin-cover.jpg"])
    numeric_movie, numeric_thin = create_movie("051325_100", "thin-cover.jpg")
    numeric_path = write_pack("051325_100", ["cover.jpg", "thin-cover.jpg"])
    kept_movie, kept_thin = create_movie("SSIS-001", "thin-cover.jpg")
    kept_path = write_pack("SSIS-001", ["cover.jpg", "thin-cover.jpg"])
    plot_movie, plot_thin = create_movie("FC2-999999", "plot-0.jpg")

    migration = _load_migration_module(
        Path(f"{REMOVE_GENERATED_THIN_COVER_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)

    for movie, thin in (
        (packed_movie, packed_thin),
        (loose_movie, loose_thin),
        (western_movie, western_thin),
        (numeric_movie, numeric_thin),
    ):
        assert Movie.get_by_id(movie.id).thin_cover_image_id is None
        assert Image.get_or_none(Image.id == thin.id) is None

    assert pack_entries(packed_path) == ["cover.jpg"]
    assert not (loose_dir / "thin-cover.webp").exists()
    assert (loose_dir / "cover.webp").exists()
    assert pack_entries(western_path) == ["cover.jpg"]
    assert pack_entries(numeric_path) == ["cover.jpg"]

    assert Movie.get_by_id(kept_movie.id).thin_cover_image_id == kept_thin.id
    assert Image.get_or_none(Image.id == kept_thin.id) is not None
    assert sorted(pack_entries(kept_path)) == ["cover.jpg", "thin-cover.jpg"]

    assert Movie.get_by_id(plot_movie.id).thin_cover_image_id == plot_thin.id
    assert Image.get_or_none(Image.id == plot_thin.id) is not None

    # 幂等：重跑不改变任何状态。
    migration.migrate(clean_db)
    assert Movie.get_by_id(packed_movie.id).thin_cover_image_id is None
    assert Movie.get_by_id(kept_movie.id).thin_cover_image_id == kept_thin.id



def test_detach_user_asset_images_migration(clean_db, monkeypatch, tmp_path):
    import zipfile

    from src.config.config import settings
    from src.model import (
        Image,
        MediaPoint,
        MediaThumbnail,
        VideoItem,
    )
    from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService

    clean_db.create_tables(TEST_MODELS)
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    library = MediaLibrary.create(name="detach", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="DETACH-001", title="detach")
    media = Media.create(movie=movie, library=library, file_name="detach.mp4")
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    thumbnails_dir.mkdir(parents=True)
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("10.webp", b"thumb-10")
        archive.writestr("20.webp", b"thumb-20")

    origin_10 = (thumbnails_dir / "10.webp").relative_to(image_root).as_posix()
    origin_20 = (thumbnails_dir / "20.webp").relative_to(image_root).as_posix()
    image_10 = Image.create(origin=origin_10)
    image_20 = Image.create(origin=origin_20)
    MediaThumbnail.create(media=media, image=image_10, offset=10)
    MediaThumbnail.create(media=media, image=image_20, offset=20)
    point_a = MediaPoint.create(
        image=image_10, movie_number="DETACH-001", offset_seconds=10
    )
    point_b = MediaPoint.create(
        image=image_10, movie_number="DETACH-001", offset_seconds=11
    )
    video = VideoItem.create(title="detach video", cover_image=image_20)

    # 来源媒体已删除的遗留时刻：图片行无 thumbnail 引用，字节为松散单文件。
    orphan_origin = "movies/yy/GONE-001/media/999/thumbnails/30.webp"
    orphan_path = image_root / orphan_origin
    orphan_path.parent.mkdir(parents=True)
    orphan_path.write_bytes(b"orphan-30")
    orphan_image = Image.create(origin=orphan_origin)
    orphan_point = MediaPoint.create(
        image=orphan_image, movie_number="GONE-001", offset_seconds=30
    )

    migration = _load_migration_module(
        Path(f"{DETACH_USER_ASSET_IMAGES_MIGRATION_NAME}.py")
    )
    migration.migrate(clean_db)

    # 时刻：共享同图的时刻复用一个新副本，字节拷出，来源缩略图行与包保留。
    detached_a = MediaPoint.get_by_id(point_a.id)
    detached_b = MediaPoint.get_by_id(point_b.id)
    assert detached_a.image_id == detached_b.image_id
    assert detached_a.image.origin.startswith("media_points/")
    assert (image_root / detached_a.image.origin).read_bytes() == b"thumb-10"
    assert Image.get_or_none(Image.id == image_10.id) is not None
    assert Image.get_or_none(Image.id == image_20.id) is not None
    with zipfile.ZipFile(pack_path) as archive:
        assert sorted(archive.namelist()) == ["10.webp", "20.webp"]

    # 视频封面：独立拷贝。
    detached_cover = VideoItem.get_by_id(video.id).cover_image
    assert detached_cover.origin.startswith(f"videos/{video.id}/cover/")
    assert (image_root / detached_cover.origin).read_bytes() == b"thumb-20"

    # 遗留孤儿时刻：旧行删除、松散文件回收，新副本可用。
    detached_orphan = MediaPoint.get_by_id(orphan_point.id)
    assert detached_orphan.image.origin.startswith("media_points/")
    assert Image.get_or_none(Image.id == orphan_image.id) is None
    assert not orphan_path.exists()

    # 幂等：重跑不改变状态。
    reused_image_ids = (
        MediaPoint.get_by_id(point_a.id).image_id,
        MediaPoint.get_by_id(point_b.id).image_id,
        MediaPoint.get_by_id(orphan_point.id).image_id,
        VideoItem.get_by_id(video.id).cover_image_id,
    )
    migration.migrate(clean_db)
    assert (
        MediaPoint.get_by_id(point_a.id).image_id,
        MediaPoint.get_by_id(point_b.id).image_id,
        MediaPoint.get_by_id(orphan_point.id).image_id,
        VideoItem.get_by_id(video.id).cover_image_id,
    ) == reused_image_ids
