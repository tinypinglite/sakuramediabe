import hashlib
import hmac
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext

JOYTAG_INFER_APP_PATH = Path(__file__).resolve().parents[1] / "docker/joytag-infer/app"
if JOYTAG_INFER_APP_PATH.exists():
    # 推理服务已从主 src 抽离，测试时显式加入独立服务源码路径。
    sys.path.insert(0, str(JOYTAG_INFER_APP_PATH))

from src.common import runtime_time
from src.config.config import Database, settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    BackgroundTaskRun,
    ClipCollection,
    ClipCollectionItem,
    DailyRecommendationItem,
    DownloadClient,
    DownloadTask,
    HotReviewItem,
    Image,
    ImageSearchSession,
    RankingItem,
    Indexer,
    ImportJob,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    MomentRecommendation,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieSimilarity,
    MovieTag,
    Subtitle,
    Playlist,
    PlaylistMovie,
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
    VideoItem,
    ResourceTaskState,
    SchemaMigration,
    Tag,
    SystemEvent,
    SystemNotification,
    User,
    UserRefreshToken,
)
from src.model.base import create_database, database_proxy, init_database

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
    VideoItem,
    VideoCollection,
    VideoCollectionItem,
    Playlist,
    PlaylistMovie,
    MediaLibrary,
    Media,
    MediaThumbnail,
    MediaProgress,
    MediaPoint,
    MediaClip,
    ClipCollection,
    ClipCollectionItem,
    MomentRecommendation,
    ImageSearchSession,
    RankingItem,
    HotReviewItem,
    DailyRecommendationItem,
    BackgroundTaskRun,
    ResourceTaskState,
    SchemaMigration,
    SystemNotification,
    SystemEvent,
    DownloadClient,
    Indexer,
    DownloadTask,
    ImportJob,
    VideoImportJob,
]


# 本地测试库连接串（含账号密码）不入库：优先读真实环境变量，缺失时回退项目根 .env.test，
# .env.test 已在 .gitignore 中，仓库只保留脱敏模板 .env.test.example。
_LOCAL_TEST_ENV_FILE = Path(__file__).resolve().parents[1] / ".env.test"


def _load_local_test_env() -> None:
    if not _LOCAL_TEST_ENV_FILE.is_file():
        return
    for raw_line in _LOCAL_TEST_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 真实环境变量优先，本地文件只补未显式设置的键。
        os.environ.setdefault(key, value)


def _require_test_database_url() -> str:
    _load_local_test_env()
    database_url = os.environ.get("SAKURAMEDIA_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.fail(
            "SAKURAMEDIA_TEST_DATABASE_URL is required for database tests. "
            "Set it in the environment or in a local .env.test file "
            "(copy .env.test.example), for example "
            "postgresql://sakuramedia:sakuramedia@127.0.0.1:5432/sakuramedia_test",
            pytrace=False,
        )
    return database_url


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _worker_database_prefix() -> str:
    # 前缀带 worker 名，清理残留时只删本 worker 的历史库，绝不误删并行中其他 worker 的库。
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    normalized_worker = re.sub(r"[^a-zA-Z0-9_]+", "_", worker_id).strip("_").lower() or "gw0"
    return f"sakuramedia_test_{normalized_worker}_"


def _build_worker_database_name() -> str:
    # 每个 xdist worker 用独立数据库，避免并行用例互相清库；名字带 uuid 防止复用历史残留。
    return f"{_worker_database_prefix()}{uuid.uuid4().hex}"


def _database_url_with_dbname(database_url: str, database_name: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", parts.query, parts.fragment))


def _run_maintenance_statements(database_url: str, statements: list[str]) -> None:
    # CREATE / DROP DATABASE 只能在 autocommit 下执行（不能处于事务块中）。
    control_database = create_database(Database(url=database_url))
    control_database.connect()
    try:
        connection = control_database.connection()
        connection.autocommit = True
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)
        cursor.close()
    finally:
        control_database.close()


def _drop_stale_worker_databases(database_url: str, prefix: str) -> None:
    # 建本次库前，清掉上次异常中断残留的同 worker 前缀库；本次库尚未创建，不会误删。
    control_database = create_database(Database(url=database_url))
    control_database.connect()
    try:
        connection = control_database.connection()
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{prefix}%",))
        stale_names = [row[0] for row in cursor.fetchall()]
        for name in stale_names:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cursor.execute(f"DROP DATABASE IF EXISTS {_quote_identifier(name)}")
        cursor.close()
    finally:
        control_database.close()


@pytest.fixture(scope="session")
def _worker_test_database_url():
    # 生产用 public schema，而 peewee 的表自省（get_tables / get_columns / get_indexes 等）在
    # PostgreSQL 下固定查 public，忽略 search_path；为与生产一致，测试为每个 worker 建独立数据库，
    # 把表建在其 public schema，而不是靠自定义 schema 隔离。
    base_url = _require_test_database_url()
    _drop_stale_worker_databases(base_url, _worker_database_prefix())
    database_name = _build_worker_database_name()
    quoted_name = _quote_identifier(database_name)
    # template0 规避目标服务器 template1 的 collation 版本不一致问题。
    _run_maintenance_statements(base_url, [f"CREATE DATABASE {quoted_name} TEMPLATE template0"])
    try:
        yield _database_url_with_dbname(base_url, database_name)
    finally:
        # 先断开该库上残留连接，再删库，避免 DROP DATABASE 被占用连接阻塞。
        _run_maintenance_statements(
            base_url,
            [
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{database_name}' AND pid <> pg_backend_pid()",
                f"DROP DATABASE IF EXISTS {quoted_name}",
            ],
        )


@pytest.fixture()
def test_db(_worker_test_database_url):
    worker_url = _worker_test_database_url
    # 每个用例重置 worker 库的 public schema，得到干净空库。
    _run_maintenance_statements(
        worker_url,
        ["DROP SCHEMA IF EXISTS public CASCADE", "CREATE SCHEMA public"],
    )

    original_database_settings = settings.database
    settings.database = Database(url=worker_url)
    database = init_database(settings.database)
    database.connect()
    for model in TEST_MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)
    try:
        yield database
    finally:
        if not database.is_closed():
            database.close()
        database_proxy.initialize(database)
        # 还原所有测试模型到全局 proxy：用例里 test_db.bind(...) 会把模型绑死到本用例的库，
        # 若不复位，同一进程后续用例（尤其是 pytest-xdist 并行打乱执行顺序时）会命中已关闭的旧库而失败。
        for model in TEST_MODELS:
            model.bind(database_proxy, bind_refs=False, bind_backrefs=False)
        settings.database = original_database_settings


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
