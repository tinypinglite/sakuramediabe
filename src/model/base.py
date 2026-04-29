import json
from urllib.parse import urlparse
from typing import Any

from peewee import (
    DatabaseProxy,
    Entity,
    Model,
    MySQLDatabase,
    NodeList,
    PostgresqlDatabase,
    SQL,
    SqliteDatabase,
    CharField,
    TextField,
)

from src.config.config import Database, DatabaseEngine

database_proxy = DatabaseProxy()


def _default_port(engine: DatabaseEngine) -> int:
    if engine is DatabaseEngine.MYSQL:
        return 3306
    if engine is DatabaseEngine.POSTGRES:
        return 5432
    raise ValueError(f"Unsupported database engine: {engine}")


def create_database(config: Database):
    if config.engine is DatabaseEngine.SQLITE:
        return SqliteDatabase(config.path, pragmas=config.pragmas)

    if not config.url:
        raise ValueError(
            f"Database url is required when engine is {config.engine.value}"
        )

    parsed = urlparse(config.url)
    database_name = parsed.path.lstrip("/")
    username = parsed.username or ""
    password = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or _default_port(config.engine)

    if config.engine is DatabaseEngine.MYSQL:
        return MySQLDatabase(
            database_name,
            user=username,
            password=password,
            host=host,
            port=port,
            charset=config.charset,
        )

    if config.engine is DatabaseEngine.POSTGRES:
        return PostgresqlDatabase(
            database_name,
            user=username,
            password=password,
            host=host,
            port=port,
        )

    raise ValueError(f"Unsupported database engine: {config.engine}")


def init_database(config: Database):
    database = create_database(config)
    database_proxy.initialize(database)
    return database


def get_database():
    if database_proxy.obj is None:
        raise RuntimeError("Database has not been initialized")
    return database_proxy.obj


class CaseSensitiveCharField(CharField):
    mysql_collation = "utf8mb4_bin"

    def _bound_database(self, ctx):
        if ctx and getattr(ctx.state, "database", None) is not None:
            return ctx.state.database
        model_database = getattr(getattr(self, "model", None), "_meta", None)
        if model_database is None:
            return None
        database = model_database.database
        if isinstance(database, DatabaseProxy):
            return database.obj
        return database

    def ddl(self, ctx):
        accum = [Entity(self.column_name)]
        data_type = self.ddl_datatype(ctx)
        if data_type:
            accum.append(data_type)
        if self.unindexed:
            accum.append(SQL("UNINDEXED"))
        if not self.null:
            accum.append(SQL("NOT NULL"))
        if self.primary_key:
            accum.append(SQL("PRIMARY KEY"))
        if self.sequence:
            accum.append(SQL("DEFAULT NEXTVAL('%s')" % self.sequence))
        if self.constraints:
            accum.extend(self.constraints)

        collation = self.collation
        if isinstance(self._bound_database(ctx), MySQLDatabase):
            collation = self.mysql_collation
        if collation:
            accum.append(SQL("COLLATE %s" % collation))
        return NodeList(accum)


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


class BaseModel(Model):
    class Meta:
        database = database_proxy
