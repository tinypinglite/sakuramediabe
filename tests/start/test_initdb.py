from src.config.config import DatabaseEngine
from src.model import (
    Actor,
    Movie,
    MoviePlotImage,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    User,
    UserRefreshToken,
)
from src.start.initdb import create_tables, init_system_playlists, init_user


def test_create_tables_creates_system_tables(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)
    create_tables()

    assert User.table_exists()
    assert UserRefreshToken.table_exists()
    assert MoviePlotImage.table_exists()
    assert Playlist.table_exists()


def test_create_tables_creates_movie_columns_for_new_schema(test_db, monkeypatch):
    monkeypatch.setattr("src.start.initdb.settings.database.engine", DatabaseEngine.SQLITE)
    monkeypatch.setattr("src.start.initdb.settings.database.path", test_db.database)

    create_tables()

    assert Movie.table_exists()
    assert Actor.table_exists()
    assert "javdb_id" in Movie._meta.fields
    assert "extra" in Movie._meta.fields
    assert "watched_count" in Movie._meta.fields
    assert "subscribed_at" in Movie._meta.fields
    assert "javdb_type" in Actor._meta.fields
    assert "subscribed_movies_synced_at" in Actor._meta.fields
    assert "subscribed_movies_full_synced_at" in Actor._meta.fields


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
