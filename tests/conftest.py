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
from src.common.file_signatures import (
    FILE_SIGNATURE_ALIGN_WINDOW_SECONDS,
    FILE_SIGNATURE_EXPIRE_SECONDS,
)
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
    IndexerDownloadClient,
    ImportJob,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
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
# 与 build_signature_expires() 保持同一套窗口对齐算法，避免生产改窗口后测试固定值漂移。
TEST_FILE_SIGNATURE_EXPIRES = -(
    -(TEST_FILE_SIGNATURE_NOW + FILE_SIGNATURE_EXPIRE_SECONDS)
    // FILE_SIGNATURE_ALIGN_WINDOW_SECONDS
) * FILE_SIGNATURE_ALIGN_WINDOW_SECONDS

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
    IndexerDownloadClient,
    DownloadTask,
    ImportJob,
    VideoImportJob,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
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


@pytest.fixture(scope="session")
def _prepared_test_database(_worker_test_database_url):
    # session 只做一次：初始化 worker 库、绑定所有测试模型、建全套表。
    # 每个用例只清空数据（TRUNCATE），避免反复执行 DROP SCHEMA + CREATE TABLE 40+ 张的巨额 DDL 开销。
    worker_url = _worker_test_database_url
    original_database_settings = settings.database
    settings.database = Database(url=worker_url)
    database = init_database(settings.database)
    database.connect()
    for model in TEST_MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)
    database.create_tables(TEST_MODELS)
    try:
        yield database
    finally:
        if not database.is_closed():
            database.close()
        settings.database = original_database_settings


def _public_schema_table_names(database) -> list[str]:
    cursor = database.execute_sql(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return [row[0] for row in cursor.fetchall()]


def _truncate_all_public_tables(database) -> None:
    tables = _public_schema_table_names(database)
    if not tables:
        return
    quoted = ", ".join(_quote_identifier(name) for name in tables)
    # RESTART IDENTITY：与旧的 drop+create schema 语义等价——每个用例从 id=1 开始。
    # CASCADE：一并处理外键关系，一次搞定所有表，比逐表 DELETE 快得多。
    database.execute_sql(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")


@pytest.fixture()
def test_db(_prepared_test_database):
    database = _prepared_test_database
    _truncate_all_public_tables(database)
    # 部分用例会调用 test_db.create_tables([...])：peewee 默认 safe=True (CREATE TABLE IF NOT EXISTS)，
    # 在完整 schema 上重复调用是无害的 no-op；TRUNCATE 已保证数据洁净。
    yield database


@pytest.fixture()
def clean_db(_worker_test_database_url, _prepared_test_database):
    # 给 test_migrations.py / test_initdb.py 这类主动测 schema 演化的用例：需要真正的空 schema。
    # 保留旧的 drop+create schema 语义；用例结束后恢复完整 schema，让后续 test_db 用例继续复用 session 库。
    worker_url = _worker_test_database_url
    session_database = _prepared_test_database
    # session_database 上可能有连接持有 public schema，DROP SCHEMA 会被阻塞。先断开。
    if not session_database.is_closed():
        session_database.close()
    _run_maintenance_statements(
        worker_url,
        ["DROP SCHEMA IF EXISTS public CASCADE", "CREATE SCHEMA public"],
    )
    session_database.connect()
    for model in TEST_MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)
    database_proxy.initialize(session_database)
    try:
        yield session_database
    finally:
        if not session_database.is_closed():
            session_database.close()
        _run_maintenance_statements(
            worker_url,
            ["DROP SCHEMA IF EXISTS public CASCADE", "CREATE SCHEMA public"],
        )
        session_database.connect()
        for model in TEST_MODELS:
            model.bind(database_proxy, bind_refs=False, bind_backrefs=False)
        database_proxy.initialize(session_database)
        session_database.create_tables(TEST_MODELS)


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

    # schema 由 _prepared_test_database 一次性建好；test_db 已经 TRUNCATE 清数据。
    # 这里不再 create_tables / drop_tables，去掉每用例 40+ 表的 DDL 大头。
    monkeypatch.setattr(settings.auth, "secret_key", "test-secret-key")
    monkeypatch.setattr(settings.auth, "access_token_expire_minutes", 60)
    monkeypatch.setattr(settings.auth, "refresh_token_expire_minutes", 60 * 24 * 7, raising=False)
    monkeypatch.setattr("src.api.app.recover_interrupted_tasks", lambda **kwargs: [])

    application = create_app()
    yield application


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
