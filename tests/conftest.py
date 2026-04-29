import hashlib
import hmac
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext

JOYTAG_INFER_APP_PATH = Path(__file__).resolve().parents[1] / "docker/joytag-infer/app"
if JOYTAG_INFER_APP_PATH.exists():
    # 推理服务已从主 src 抽离，测试时显式加入独立服务源码路径。
    sys.path.insert(0, str(JOYTAG_INFER_APP_PATH))

from src.common import runtime_time
from src.config.config import Database, DatabaseEngine, settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    HotReviewItem,
    Image,
    ImageSearchSession,
    RankingItem,
    Indexer,
    ImportJob,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieSimilarity,
    MovieTag,
    Subtitle,
    Playlist,
    PlaylistMovie,
    ResourceTaskState,
    SchemaMigration,
    Tag,
    SystemEvent,
    SystemNotification,
    User,
    UserRefreshToken,
)
from src.model.base import database_proxy, init_database

PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")
TEST_FILE_SIGNATURE_SECRET = "test-file-secret"
TEST_FILE_SIGNATURE_NOW = 1700000000
TEST_FILE_SIGNATURE_EXPIRES = TEST_FILE_SIGNATURE_NOW + 12 * 60 * 60

TEST_MODELS = [
    User,
    UserRefreshToken,
    Image,
    Tag,
    Actor,
    MovieSeries,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSimilarity,
    MovieTag,
    Subtitle,
    Playlist,
    PlaylistMovie,
    MediaLibrary,
    Media,
    MediaThumbnail,
    MediaProgress,
    MediaPoint,
    ImageSearchSession,
    RankingItem,
    HotReviewItem,
    BackgroundTaskRun,
    ResourceTaskState,
    SchemaMigration,
    SystemNotification,
    SystemEvent,
    DownloadClient,
    Indexer,
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


@pytest.fixture(autouse=True)
def fake_default_dmm_provider(monkeypatch):
    from src.service.catalog.catalog_import_service import CatalogImportService

    class _FakeDmmProvider:
        def get_movie_desc(self, movie_number: str) -> str:
            raise MetadataNotFoundError("movie_desc", movie_number)

    monkeypatch.setattr(
        CatalogImportService,
        "_build_dmm_provider",
        staticmethod(lambda: _FakeDmmProvider()),
    )


@pytest.fixture()
def app(test_db, monkeypatch):
    from src.api.app import create_app

    test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(TEST_MODELS)
    monkeypatch.setattr(settings.auth, "secret_key", "test-secret-key")
    monkeypatch.setattr(settings.auth, "access_token_expire_minutes", 60)
    monkeypatch.setattr(settings.auth, "refresh_token_expire_minutes", 60 * 24 * 7, raising=False)
    monkeypatch.setattr("src.api.app.recover_interrupted_tasks", lambda **kwargs: [])

    application = create_app()
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


@pytest.fixture(autouse=True)
def fixed_runtime_timezone(monkeypatch):
    # 测试统一锁定到 UTC，避免断言结果受执行机器本地时区影响。
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    runtime_time.clear_runtime_timezone_cache()
    yield
    runtime_time.clear_runtime_timezone_cache()


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
    def _build(subtitle_id: int, expires: int = TEST_FILE_SIGNATURE_EXPIRES) -> str:
        signature_payload = f"subtitles:{subtitle_id}:{expires}"
        signature = hmac.new(
            TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"/files/subtitles/{subtitle_id}?expires={expires}&signature={signature}"

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
