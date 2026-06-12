from datetime import date, datetime

from peewee import IntegrityError

from src.config.config import DatabaseEngine
from src.model import (
    Actor,
    BackgroundTaskRun,
    DailyRecommendationItem,
    Image,
    Media,
    MediaThumbnail,
    MomentRecommendation,
    HotReviewItem,
    Movie,
    MoviePlotImage,
    MovieSeries,
    MovieSimilarity,
    PLAYLIST_KIND_4K,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_VR,
    Playlist,
    RankingItem,
    ResourceTaskState,
    SchemaMigration,
    Subtitle,
    SystemEvent,
    SystemNotification,
    Person,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
    VideoItemPerson,
    VideoItemTag,
    User,
    UserRefreshToken,
)
from src.start.initdb import create_tables, init_system_playlists, init_user, initdb
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


def test_create_tables_creates_system_tables(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    assert User.table_exists()
    assert UserRefreshToken.table_exists()
    assert MoviePlotImage.table_exists()
    assert MovieSeries.table_exists()
    assert MovieSimilarity.table_exists()
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


def test_create_tables_creates_videos_domain_tables_and_decoupled_media(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    database = create_tables()

    assert Person.table_exists()
    assert VideoItem.table_exists()
    assert VideoItemPerson.table_exists()
    assert VideoItemTag.table_exists()
    assert VideoCollection.table_exists()
    assert VideoCollectionItem.table_exists()

    # 合集成员带 position 排序字段。
    collection_item_columns = {
        row[1] for row in database.execute_sql("PRAGMA table_info(video_collection_item)").fetchall()
    }
    assert "position" in collection_item_columns

    # Media 解耦：movie_number 可空且新增 video_item_id。
    media_info = {row[1]: row for row in database.execute_sql("PRAGMA table_info(media)").fetchall()}
    assert "video_item_id" in media_info
    # PRAGMA table_info 第 3 列(notnull) 为 0 表示可空。
    assert media_info["movie_number"][3] == 0


def test_create_tables_creates_daily_recommendation_unique_constraints(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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


def test_create_tables_creates_moment_recommendation_unique_constraints(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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


def test_create_tables_creates_current_schema_columns(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)
    # 组合运行时其他测试可能重绑 Peewee 模型；这里显式绑定当前库再验证实际建表结果。
    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)

    database = create_tables()
    if database.is_closed():
        database.connect()

    assert Actor.table_exists()
    actor_columns = {row[1] for row in database.execute_sql("PRAGMA table_info(actor)").fetchall()}
    assert "subscribed_at" in actor_columns
    assert BackgroundTaskRun.table_exists()
    assert ResourceTaskState.table_exists()


def test_create_tables_creates_resource_task_state_unique_constraint(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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


def test_create_tables_creates_background_task_run_mutex_index_for_new_schema(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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


def test_create_tables_does_not_patch_existing_legacy_movie_schema(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)
    # 当前受支持的老用户 schema 只缺少 title_zh。
    _create_movie_table_missing_title_zh(test_db)

    create_tables()

    movie_columns = {column.name for column in test_db.get_columns("movie")}

    assert "maker_name" in movie_columns
    assert "director_name" in movie_columns
    assert "desc" in movie_columns
    assert "desc_zh" in movie_columns
    assert "title_zh" not in movie_columns
    assert "series_id" not in movie_columns
    assert "is_collection_overridden" in movie_columns


def test_create_tables_creates_movie_series_schema(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    assert MovieSeries.table_exists()
    assert "name" in MovieSeries._meta.fields
    assert "series" in Movie._meta.fields
    assert "series_name" not in Movie._meta.fields


def test_init_user_creates_single_account_once(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.auth.username", "account")
    monkeypatch.setattr("src.start.initdb.settings.auth.password", "account")
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    created = init_user()
    repeated = init_user()

    account = User.get(User.username == "account")

    assert created is True
    assert repeated is False
    assert account.username == "account"
    assert "role" not in User._meta.fields
    assert User.select().count() == 1


def test_init_system_playlists_creates_all_system_playlists_once(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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


def test_init_system_playlists_backfills_missing_kinds_on_upgrade(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

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
