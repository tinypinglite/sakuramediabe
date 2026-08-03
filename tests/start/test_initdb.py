from datetime import date, datetime

import pytest
from peewee import IntegrityError, ProgrammingError

from src.model import (
    PLAYLIST_KIND_4K,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_VR,
    Actor,
    BackgroundTaskRun,
    DailyRecommendationItem,
    DownloadClient,
    HotReviewItem,
    Image,
    IndexerDownloadClient,
    Media,
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    MediaThumbnail,
    MomentRecommendation,
    Movie,
    MoviePlotImage,
    MovieSeries,
    Playlist,
    RankingItem,
    ResourceTaskAttempt,
    ResourceTaskState,
    SchemaMigration,
    Subtitle,
    SystemEvent,
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
    assert ResourceTaskState.table_exists()
    assert SchemaMigration.table_exists()
    assert SystemNotification.table_exists()
    assert SystemEvent.table_exists()
    assert Subtitle.table_exists()
    assert MediaRapidUploadBatch.table_exists()
    assert MediaRapidUploadItem.table_exists()
    # 秒传 item 需带 failure_reason 列，用来区分 not_hit / 其它可重试失败。
    rapid_upload_item_columns = _column_names(clean_db, "media_rapid_upload_item")
    assert "failure_reason" in rapid_upload_item_columns
    assert _column_is_nullable(clean_db, "media_rapid_upload_item", "failure_reason") is True
    # media_id 单列索引：list_media 批量查最新秒传状态的 DISTINCT ON 依赖它。
    rapid_upload_item_indexed_columns = {
        tuple(index.columns)
        for index in clean_db.get_indexes("media_rapid_upload_item")
    }
    assert ("media_id",) in rapid_upload_item_indexed_columns


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
    first_media = Media.create(movie=first_movie, path="/library/moment-1.mp4")
    second_media = Media.create(movie=second_movie, path="/library/moment-2.mp4")
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
    assert ResourceTaskState.table_exists()


def test_create_tables_creates_resource_task_state_unique_constraint(clean_db, monkeypatch):
    create_tables()

    ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=1,
    )
    try:
        ResourceTaskState.create(
            task_key="movie_desc_sync",
            resource_type="movie",
            resource_id=1,
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("expected resource_task_state unique constraint to reject duplicate rows")


def test_create_tables_creates_background_task_run_mutex_index_for_new_schema(clean_db, monkeypatch):
    create_tables()

    BackgroundTaskRun.create(
        task_key="ranking_sync",
        task_name="排行榜同步",
        trigger_type="scheduled",
        mutex_key="aps:ranking_sync",
    )

    try:
        BackgroundTaskRun.create(
            task_key="ranking_sync",
            task_name="排行榜同步",
            trigger_type="manual",
            mutex_key="aps:ranking_sync",
        )
    except IntegrityError:
        pass
    else:
        raise AssertionError("expected mutex_key unique constraint to reject duplicate rows")


def test_create_tables_creates_task_queue_and_attempt_schema(clean_db, monkeypatch):
    """任务架构 Wave 0：队列扩列、尝试历史表、投影扩列与外键化（存量库走 20260729_01 迁移）。"""
    create_tables()

    assert ResourceTaskAttempt.table_exists()
    run_columns = _column_names(clean_db, "background_task_run")
    assert {"params", "scheduled_at", "lease_expires_at"} <= run_columns
    state_columns = _column_names(clean_db, "resource_task_state")
    assert {"next_retry_at", "error_code", "retry_round", "last_attempt_id"} <= state_columns

    # 队列领取与重试调度的两个组合索引。
    run_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("background_task_run")
    }
    assert ("state", "scheduled_at") in run_indexed_columns
    state_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("resource_task_state")
    }
    assert ("task_key", "state", "next_retry_at") in state_indexed_columns

    # 尝试历史清理走 finished_at 索引定位过期行，缺索引会全表扫。
    attempt_indexed_columns = {
        tuple(index.columns) for index in clean_db.get_indexes("resource_task_attempt")
    }
    assert ("finished_at",) in attempt_indexed_columns

    # last_task_run_id 外键化：悬空引用必须被数据库拒绝。
    with pytest.raises(IntegrityError):
        ResourceTaskState.create(
            task_key="movie_desc_sync",
            resource_type="movie",
            resource_id=42,
            last_task_run_id=999_999,
        )


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
    """人工输入点查依赖的 UPPER(movie_number) 函数索引：新库建表即有，存量库走 20260728_01 迁移。"""
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


def test_init_system_playlists_creates_all_system_playlists_once(clean_db, monkeypatch):
    create_tables()

    created = init_system_playlists()
    repeated = init_system_playlists()

    kinds = {playlist.kind for playlist in Playlist.select()}

    assert created is True
    assert repeated is False
    assert kinds == {PLAYLIST_KIND_RECENTLY_PLAYED, PLAYLIST_KIND_VR, PLAYLIST_KIND_4K}
    assert Playlist.get(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED).name == "最近播放"
    assert Playlist.get(Playlist.kind == PLAYLIST_KIND_VR).name == "VR"
    assert Playlist.get(Playlist.kind == PLAYLIST_KIND_4K).name == "4K"
    assert Playlist.select().count() == 3


def test_init_system_playlists_backfills_missing_kinds_on_upgrade(clean_db, monkeypatch):
    create_tables()
    # 显式构造"老库仅有最近播放"的初始状态，不依赖建表后表为空。
    Playlist.delete().execute()
    Playlist.create(kind=PLAYLIST_KIND_RECENTLY_PLAYED, name="最近播放", description="")

    created = init_system_playlists()

    kinds = {playlist.kind for playlist in Playlist.select()}
    assert created is True
    assert kinds == {PLAYLIST_KIND_RECENTLY_PLAYED, PLAYLIST_KIND_VR, PLAYLIST_KIND_4K}
    assert Playlist.select().count() == 3


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

    # 中间表建出且 download_client 带 kind、download_task 带 target_ref。
    assert IndexerDownloadClient.table_exists()
    client_columns = {column.name for column in database.get_columns("download_client")}
    assert "kind" in client_columns
    task_columns = {column.name for column in database.get_columns("download_task")}
    assert "target_ref" in task_columns
    cloud115_indexes = {
        index.name: index
        for index in database.get_indexes("download_client")
        if index.unique
    }
    assert "download_client_cloud115_library_unique" in cloud115_indexes


def test_create_tables_cloud115_client_index_is_partial(clean_db, monkeypatch):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    create_tables()
    local_library = MediaLibrary.create(
        name="local-downloads", backend="local", backend_config={"root_path": "/library"}
    )
    cloud_library = MediaLibrary.create(
        name="cloud-downloads", backend="cloud115", backend_config={"cookies": "x"}
    )

    DownloadClient.create(name="qb-a", kind="qbittorrent", media_library=local_library)
    DownloadClient.create(name="qb-b", kind="qbittorrent", media_library=local_library)
    DownloadClient.create(name="cloud-a", kind="cloud115", media_library=cloud_library)
    with pytest.raises(IntegrityError):
        DownloadClient.create(name="cloud-b", kind="cloud115", media_library=cloud_library)
