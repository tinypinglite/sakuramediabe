import json
from typing import Any
from urllib.parse import parse_qsl, urlparse

from peewee import (
    CharField,
    DatabaseProxy,
    Model,
    PostgresqlDatabase,
    TextField,
)

from src.config.config import Database, DatabaseEngine

database_proxy = DatabaseProxy()


def create_database(config: Database):
    if config.engine is not DatabaseEngine.POSTGRES:
        raise ValueError(f"Unsupported database engine: {config.engine}")

    if not config.url:
        raise ValueError("Database url is required when engine is postgres")

    parsed = urlparse(config.url)
    database_name = parsed.path.lstrip("/")
    username = parsed.username or ""
    password = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    connect_options = dict(parse_qsl(parsed.query, keep_blank_values=True))

    return PostgresqlDatabase(
        database_name,
        user=username,
        password=password,
        host=host,
        port=port,
        **connect_options,
    )


def init_database(config: Database):
    database = create_database(config)
    database_proxy.initialize(database)
    return database


def get_database():
    if database_proxy.obj is None:
        raise RuntimeError("Database has not been initialized")
    return database_proxy.obj


class CaseSensitiveCharField(CharField):
    """PostgreSQL 默认字符串比较区分大小写，保留类型名表达领域语义。"""


class JsonTextField(TextField):
    def db_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def python_value(self, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


class JsonbField(TextField):
    """PostgreSQL JSONB 列；存量 JSONB 字段结构迁移等场景使用（列类型按文档约定为 jsonb）。"""

    field_type = "JSONB"

    def db_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def python_value(self, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


class BaseModel(Model):
    class Meta:
        database = database_proxy
