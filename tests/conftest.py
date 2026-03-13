import hashlib
import hmac
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext

from src.config.config import Database, DatabaseEngine, settings
from src.model import (
    Actor,
    DownloadClient,
    DownloadTask,
    Image,
    ImageSearchSession,
    ImportJob,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
    User,
    UserRefreshToken,
)
from src.model.base import database_proxy, init_database

PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")
TEST_FILE_SIGNATURE_SECRET = "test-file-secret"
TEST_FILE_SIGNATURE_NOW = 1700000000
TEST_FILE_SIGNATURE_EXPIRES = TEST_FILE_SIGNATURE_NOW + 900

TEST_MODELS = [
    User,
    UserRefreshToken,
    Image,
    Tag,
    Actor,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Playlist,
    PlaylistMovie,
    MediaLibrary,
    Media,
    MediaThumbnail,
    MediaProgress,
    MediaPoint,
    ImageSearchSession,
    DownloadClient,
    DownloadTask,
    ImportJob,
]


@pytest.fixture()
def test_db(tmp_path):
    database = init_database(
        Database(
            engine=DatabaseEngine.SQLITE,
            path=str(tmp_path / "test.sqlite"),
        )
    )
    yield database
    if not database.is_closed():
        database.close()
    database_proxy.initialize(database)


@pytest.fixture()
def app(test_db, monkeypatch):
    from src.api.app import create_app

    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)
    monkeypatch.setattr(settings.auth, "secret_key", "test-secret-key")
    monkeypatch.setattr(settings.auth, "access_token_expire_minutes", 60)
    monkeypatch.setattr(settings.auth, "refresh_token_expire_minutes", 60 * 24 * 7, raising=False)

    application = create_app(run_initdb_on_startup=False)
    yield application
    test_db.drop_tables(list(reversed(TEST_MODELS)))


@pytest.fixture(autouse=True)
def fixed_file_signature_settings(monkeypatch):
    monkeypatch.setattr(
        settings.auth,
        "file_signature_secret",
        TEST_FILE_SIGNATURE_SECRET,
        raising=False,
    )
    monkeypatch.setattr(
        settings.auth,
        "file_signature_expire_seconds",
        900,
        raising=False,
    )

    try:
        from src.common import file_signatures
    except ImportError:
        yield
        return

    monkeypatch.setattr(
        file_signatures,
        "_now_timestamp",
        lambda: TEST_FILE_SIGNATURE_NOW,
    )
    yield


@pytest.fixture()
def build_signed_image_url():
    def _build(relative_path: str, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
        signature_payload = f"images:{relative_path}:{expires}"
        signature = hmac.new(
            TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"/files/images/{quote(relative_path, safe='/')}"
            f"?expires={expires}&signature={signature}"
        )

    return _build


@pytest.fixture()
def build_signed_subtitle_url():
    def _build(media_id: int, file_name: str, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
        signature_payload = f"subtitles:{media_id}:{file_name}:{expires}"
        signature = hmac.new(
            TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"/files/subtitles/{media_id}/{quote(file_name, safe='')}"
            f"?expires={expires}&signature={signature}"
        )

    return _build


@pytest.fixture()
def build_signed_media_url():
    def _build(media_id: int, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
        signature_payload = f"media:{media_id}:{expires}"
        signature = hmac.new(
            TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"/media/{media_id}/stream?expires={expires}&signature={signature}"

    return _build


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def account_user():
    return User.create(
        username="account",
        password_hash=PASSWORD_CONTEXT.hash("password123"),
    )


@pytest.fixture()
def normal_user():
    return User.create(
        username="alice",
        password_hash=PASSWORD_CONTEXT.hash("password123"),
    )
