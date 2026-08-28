from datetime import date, datetime

import pytest
from peewee import IntegrityError, ProgrammingError

from src.model import (
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Actor,
    BackgroundTaskRun,
    DailyRecommendationItem,
    DownloadClient,
    HotReviewItem,
    Image,
    IndexerDownloadClient,
    Media,
    MediaLibrary,
    MediaThumbnail,
    MomentRecommendation,
    Movie,
    MoviePlotImage,
    MovieSeries,
    Playlist,
    RankingItem,
    SchemaMigration,
    Subtitle,
    SystemNotification,
    User,
    UserRefreshToken,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.start.initdb import create_tables, init_system_playlists, init_user, initdb
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


def _column_names(database, table_name: str) -> set[str]:
    return {column.name for column in database.get_columns(table_name)}


def _column_is_nullable(database, table_name: str, column_name: str) -> bool:
    for column in database.get_columns(table_name):
        if column.name == column_name:
            return column.null
    raise AssertionError(f"column not found: {table_name}.{column_name}")


def test_create_tables_creates_system_tables(clean_db, monkeypatch):
    create_tables()

    assert User.table_exists()
    assert UserRefreshToken.table_exists()
    assert MoviePlotImage.table_exists()
    assert MovieSeries.table_exists()
    assert DailyRecommendationItem.table_exists()
    assert RankingItem.table_exists()
    assert HotReviewItem.table_exists()
    assert Playlist.table_exists()
    assert BackgroundTaskRun.table_exists()
    assert not clean_db.table_exists("resource_task_state")
    assert not clean_db.table_exists("resource_task_attempt")
    assert SchemaMigration.table_exists()
    assert SystemNotification.table_exists()
    assert not clean_db.table_exists("system_event")
    notification_columns = _column_names(clean_db, "system_notification")
    assert {
        "event_type",
        "dedupe_key",
        "resource_type",
        "resource_id",
    } <= notification_columns
    notification_indexes = clean_db.get_indexes("system_notification")
    assert any(
        tuple(index.columns) == ("dedupe_key",) and index.unique
        for index in notification_indexes
    )
    assert Subtitle.table_exists()
    assert "is_blacklisted" in _column_names(clean_db, "movie")
    assert not clean_db.table_exists("subtitle_import_job")


def test_create_tables_creates_videos_domain_tables_and_decoupled_media(clean_db, monkeypatch):
    # 组合运行时其他测试可能重绑 Peewee 模型；这里显式绑定当前库再验证实际建表结果。
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)

    database = create_tables()

    assert VideoItem.table_exists()
    assert VideoCollection.table_exists()
    assert VideoCollectionItem.table_exists()

    # 合集成员带 position 排序字段。
    collection_item_columns = _column_names(database, "video_collection_item")
    assert "position" in collection_item_columns

    # Media 解耦：movie_number 可空且新增 video_item_id。
    media_columns = _column_names(database, "media")
    assert "video_item_id" in media_columns
    assert _column_is_nullable(database, "media", "movie_number") is True
    assert {"library_id", "storage_ref", "file_name"} <= media_columns
    assert not {"path", "backend_locator", "storage_mode"} & media_columns


def test_create_tables_creates_daily_recommendation_unique_constraints(clean_db, monkeypatch):
    create_tables()

    first_movie = Movie.create(movie_number="ABP-001", javdb_id="daily-1", title="Daily 1")
    second_movie = Movie.create(movie_number="ABP-002", javdb_id="daily-2", title="Daily 2")
    DailyRecommendationItem.create(
        snapshot_date=date(2026, 5, 8),
        movie=first_movie,
        rank=1,
        generated_at=datetime(2026, 5, 8, 5, 0, 0),
    )

    try:
        DailyRecommendationItem.create(
            snapshot_date=date(2026, 5, 8),
            movie=second_movie,
            rank=1,
            generated_at=datetime(2026, 5, 8, 5, 0, 0),
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("daily recommendation rank unique constraint missing")

    try:
        DailyRecommendationItem.create(
            snapshot_date=date(2026, 5, 8),
            movie=first_movie,
            rank=2,
            generated_at=datetime(2026, 5, 8, 5, 0, 0),
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("daily recommendation movie unique constraint missing")


def test_create_tables_creates_moment_recommendation_unique_constraints(clean_db, monkeypatch):
    create_tables()

    first_movie = Movie.create(movie_number="ABP-101", javdb_id="moment-1", title="Moment 1")
    second_movie = Movie.create(movie_number="ABP-102", javdb_id="moment-2", title="Moment 2")
    library = MediaLibrary.create(
        name="moment-library",
        provider_key="demo",
        provider_config={},
    )
    first_media = Media.create(
        movie=first_movie,
        library=library,
        storage_ref={"id": "moment-1"},
        file_name="moment-1.mp4",
    )
    second_media = Media.create(
        movie=second_movie,
        library=library,
        storage_ref={"id": "moment-2"},
        file_name="moment-2.mp4",
    )
    first_image = Image.create(origin="a.webp", small="a.webp", medium="a.webp", large="a.webp")
    second_image = Image.create(origin="b.webp", small="b.webp", medium="b.webp", large="b.webp")
    first_thumbnail = MediaThumbnail.create(media=first_media, image=first_image, offset=120)
    second_thumbnail = MediaThumbnail.create(media=second_media, image=second_image, offset=240)
    MomentRecommendation.create(
        rank=1,
        score=0.8,
        strategy="popular",
        reason="热门",
        movie=first_movie,
        media=first_media,
        thumbnail=first_thumbnail,
        offset_seconds=120,
        generated_at=datetime(2026, 5, 8, 4, 0, 0),
    )

    try:
        MomentRecommendation.create(
            rank=1,
            score=0.7,
            strategy="popular",
            reason="热门",
            movie=second_movie,
            media=second_media,
            thumbnail=second_thumbnail,
            offset_seconds=240,
            generated_at=datetime(2026, 5, 8, 4, 0, 0),
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("moment recommendation rank unique constraint missing")

    try:
        MomentRecommendation.create(
            rank=2,
            score=0.7,
            strategy="popular",
            reason="热门",
            movie=first_movie,
            media=first_media,
            thumbnail=first_thumbnail,
            offset_seconds=120,
            generated_at=datetime(2026, 5, 8, 4, 0, 0),
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("moment recommendation thumbnail unique constraint missing")


def test_create_tables_creates_current_schema_columns(clean_db, monkeypatch):
    # 组合运行时其他测试可能重绑 Peewee 模型；这里显式绑定当前库再验证实际建表结果。
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)

    database = create_tables()
    if database.is_closed():
        database.connect()

    assert Actor.table_exists()
    actor_columns = _column_names(database, "actor")
    assert "subscribed_at" in actor_columns
    assert BackgroundTaskRun.table_exists()
    # v2-lite 字段主权两列：新库按模型渲染出 JSONB / BIGINT + 服务端默认值
    # （与 v0.5.0 收敛迁移的 ALTER 同构，裸 INSERT 也有兜底）。
    movie_columns = _column_names(database, "movie")
    assert "interaction_synced_at" in movie_columns
    assert "field_owners" in movie_columns
    assert "mutation_revision" in movie_columns
    assert "is_collection_overridden" not in movie_columns
    movie_column_types = {
        column.name: column.data_type
        for column in database.get_columns("movie")
    }
    assert movie_column_types["field_owners"] == "jsonb"
    assert movie_column_types["mutation_revision"] == "bigint"
    movie_column_defaults = {
        column.name: column.default
        for column in database.get_columns("movie")
    }
    assert movie_column_defaults["field_owners"] == "'{}'::jsonb"
    assert movie_column_defaults["mutation_revision"] == "0"
    library_columns = _column_names(database, "media_library")
    assert {"provider_key", "provider_config", "account_key"} <= library_columns
    assert not {"backend", "backend_config", "backend_account_key"} & library_columns


def test_create_tables_creates_background_task_run_mutex_index_for_new_schema(clean_db, monkeypatch):
    create_tables()

    BackgroundTaskRun.create(
        task_key="movie_heat_update",
        task_name="影片热度更新",
        trigger_type="scheduled",
        mutex_key="aps:movie_heat_update",
    )

    try:
        BackgroundTaskRun.create(
            task_key="movie_heat_update",
            task_name="影片热度更新",
            trigger_type="manual",
            mutex_key="aps:movie_heat_update",
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("expected mutex_key unique constraint to reject duplicate rows")


def test_create_tables_creates_task_queue_schema(clean_db, monkeypatch):
    """任务队列保留，资源级投影与尝试历史不再进入当前 schema。"""
    create_tables()

    run_columns = _column_names(clean_db, "background_task_run")
    assert {"params", "scheduled_at", "lease_expires_at"} <= run_columns
    assert not clean_db.table_exists("resource_task_state")
    assert not clean_db.table_exists("resource_task_attempt")

    # 队列领取组合索引仍保留。
    run_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("background_task_run")
    }
    assert ("state", "scheduled_at") in run_indexed_columns


def test_create_tables_does_not_patch_existing_legacy_movie_schema(clean_db, monkeypatch):
    # 当前受支持的老用户 schema 只缺少 title_zh。
    _create_movie_table_missing_title_zh(clean_db)

    # 生产链路是先 migrate 再 initdb；老 schema 直接跑 create_tables 时，PostgreSQL 会因
    # 现有 movie 表缺少新版索引所需列（如 series_id）而报错，正好把用户挡在"必须先跑迁移"上，
    # 而不是像 SQLite 那样静默给不存在的列建索引留下奇怪状态。
    with pytest.raises(ProgrammingError):
        create_tables()

    # 关键约束：即使建表流程中断，legacy movie 表本身必须保持原样，未被 initdb 悄悄补列。
    movie_columns = {column.name for column in clean_db.get_columns("movie")}

    assert "maker_name" in movie_columns
    assert "director_name" in movie_columns
    assert "desc" in movie_columns
    assert "desc_zh" in movie_columns
    assert "title_zh" not in movie_columns
    assert "series_id" not in movie_columns
    assert "is_collection_overridden" in movie_columns


def test_create_tables_creates_movie_series_schema(clean_db, monkeypatch):
    create_tables()

    assert MovieSeries.table_exists()
    assert "name" in MovieSeries._meta.fields
    assert "series" in Movie._meta.fields
    assert "series_name" not in Movie._meta.fields


def test_create_tables_creates_movie_number_upper_index(clean_db, monkeypatch):
    """人工输入点查依赖的 UPPER(movie_number) 函数索引：新库建表即有。"""
    create_tables()

    assert clean_db.execute_sql(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'movie_movie_number_upper'"
        " AND schemaname = current_schema()"
    ).fetchone() is not None


def test_init_user_creates_single_account_once(clean_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.auth.username", "account")
    monkeypatch.setattr("src.start.initdb.settings.auth.password", "account")

    create_tables()

    created = init_user()
    repeated = init_user()

    account = User.get(User.username == "account")

    assert created is True
    assert repeated is False
    assert account.username == "account"
    assert "role" not in User._meta.fields
    assert User.select().count() == 1


def test_init_system_playlists_creates_recently_played_once(clean_db, monkeypatch):
    create_tables()

    created = init_system_playlists()
    repeated = init_system_playlists()

    kinds = {playlist.kind for playlist in Playlist.select()}

    assert created is True
    assert repeated is False
    assert kinds == {PLAYLIST_KIND_RECENTLY_PLAYED}
    assert Playlist.get(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED).name == "最近播放"
    assert Playlist.select().count() == 1


def test_init_system_playlists_does_not_restore_removed_kinds(clean_db, monkeypatch):
    create_tables()
    # 显式构造"老库仅有最近播放"的初始状态，不依赖建表后表为空。
    Playlist.delete().execute()
    Playlist.create(kind=PLAYLIST_KIND_RECENTLY_PLAYED, name="最近播放", description="")

    created = init_system_playlists()

    kinds = {playlist.kind for playlist in Playlist.select()}
    assert created is False
    assert kinds == {PLAYLIST_KIND_RECENTLY_PLAYED}
    assert Playlist.select().count() == 1


def test_initdb_does_not_run_pending_migrations(monkeypatch):
    events = []

    monkeypatch.setattr("src.start.initdb.create_tables", lambda: events.append("create_tables"))
    monkeypatch.setattr("src.start.initdb.init_user", lambda: events.append("init_user"))
    monkeypatch.setattr(
        "src.start.initdb.init_system_playlists",
        lambda: events.append("init_system_playlists"),
    )

    initdb()

    assert events == ["create_tables", "init_user", "init_system_playlists"]


def test_create_tables_creates_download_domain_multi_bind_schema(clean_db, monkeypatch):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)

    database = create_tables()
    if database.is_closed():
        database.connect()

    # 中间表建出且宿主只保留 provider-neutral download fields。
    assert IndexerDownloadClient.table_exists()
    client_columns = {column.name for column in database.get_columns("download_client")}
    assert {"library_id", "provider_config"} <= client_columns
    assert not {
        "kind",
        "base_url",
        "username",
        "password",
        "client_save_path",
        "local_root_path",
        "media_library_id",
    } & client_columns
    task_columns = {column.name for column in database.get_columns("download_task")}
    assert {"remote_id", "state", "progress", "completed_source_ref"} <= task_columns
    assert not {
        "info_hash",
        "save_path",
        "download_state",
        "raw_state",
        "download_speed_bytes",
        "uploaded_speed_bytes",
        "downloaded_bytes",
        "total_size_bytes",
        "eta_seconds",
        "progress_synced_at",
        "download_started_at",
    } & task_columns
    library = MediaLibrary.create(
        name="snapshot-defaults",
        provider_key="demo",
        provider_config={},
    )
    download_client = DownloadClient.create(
        name="snapshot-defaults-qb",
        library=library,
        provider_config={},
    )
    database.execute_sql(
        "INSERT INTO download_task"
        " (created_at, updated_at, client_id, remote_id, name, progress, state, import_status)"
        " VALUES (now(), now(), %s, 'initdb-snapshot', 'ABP-001', 0, 'queued', 'pending')",
        (download_client.id,),
    )
    snapshot_defaults = database.execute_sql(
        "SELECT completed_source_ref FROM download_task WHERE remote_id = 'initdb-snapshot'"
    ).fetchone()
    assert snapshot_defaults == (None,)
    # 每个索引器独立可选的 Torznab 鉴权 key，新库直接建出可空列。
    indexer_columns = {column.name for column in database.get_columns("indexer")}
    assert "api_key" in indexer_columns
    assert _column_is_nullable(database, "indexer", "api_key") is True
