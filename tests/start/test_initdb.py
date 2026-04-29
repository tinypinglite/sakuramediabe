from peewee import IntegrityError

from src.config.config import DatabaseEngine
from src.model import (
    Actor,
    BackgroundTaskRun,
    HotReviewItem,
    Movie,
    MoviePlotImage,
    MovieSeries,
    MovieSimilarity,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    RankingItem,
    ResourceTaskState,
    SchemaMigration,
    Subtitle,
    SystemEvent,
    SystemNotification,
    User,
    UserRefreshToken,
)
from src.start.initdb import create_tables, init_system_playlists, init_user, initdb


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
    assert RankingItem.table_exists()
    assert HotReviewItem.table_exists()
    assert Playlist.table_exists()
    assert BackgroundTaskRun.table_exists()
    assert ResourceTaskState.table_exists()
    assert SchemaMigration.table_exists()
    assert SystemNotification.table_exists()
    assert SystemEvent.table_exists()
    assert Subtitle.table_exists()


def test_create_tables_creates_current_schema_columns(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    assert Actor.table_exists()
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


def test_init_system_playlists_creates_recently_played_once(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    created = init_system_playlists()
    repeated = init_system_playlists()

    playlist = Playlist.get(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)

    assert created is True
    assert repeated is False
    assert playlist.name == "最近播放"
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
