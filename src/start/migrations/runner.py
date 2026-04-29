from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType

from peewee import Database, MySQLDatabase, PostgresqlDatabase, SqliteDatabase
from playhouse.migrate import MySQLMigrator, PostgresqlMigrator, SqliteMigrator

from src.model import SchemaMigration

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"


class SkipMigration(RuntimeError):
    """迁移前置条件尚未满足时显式跳过，避免误记为已应用。"""


@dataclass(frozen=True)
class MigrationExecution:
    name: str
    applied: bool


@dataclass(frozen=True)
class MigrationRunSummary:
    executed: list[MigrationExecution]

    @property
    def applied_count(self) -> int:
        return sum(1 for item in self.executed if item.applied)

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.executed if not item.applied)


def _build_migrator(database: Database):
    if isinstance(database, SqliteDatabase):
        return SqliteMigrator(database)
    if isinstance(database, MySQLDatabase):
        return MySQLMigrator(database)
    if isinstance(database, PostgresqlDatabase):
        return PostgresqlMigrator(database)
    raise ValueError(f"unsupported_migration_database: {type(database).__name__}")


def _load_migration_module(path: Path) -> ModuleType:
    return import_module(f"src.start.migrations.versions.{path.stem}")


def _list_migration_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modules.append(_load_migration_module(path))
    return modules


def run_pending_migrations(database: Database) -> MigrationRunSummary:
    # 迁移记录表的查询和写入必须绑定到目标数据库，避免被全局 proxy 残留状态污染。
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        # 迁移记录表由迁移命令显式托管，不依赖 initdb/aps 启动期补库。
        database.create_tables([SchemaMigration], safe=True)
        migrator = _build_migrator(database)
        applied_names = {item.name for item in SchemaMigration.select(SchemaMigration.name)}
        executed: list[MigrationExecution] = []

        for module in _list_migration_modules():
            migration_name = str(getattr(module, "name", "")).strip()
            migrate_callable = getattr(module, "migrate", None)
            if not migration_name:
                raise ValueError(f"migration_name_missing: {module.__name__}")
            if not callable(migrate_callable):
                raise ValueError(f"migration_callable_missing: {migration_name}")
            if migration_name in applied_names:
                executed.append(MigrationExecution(name=migration_name, applied=False))
                continue

            try:
                with database.atomic():
                    migrate_callable(database, migrator)
                    SchemaMigration.create(name=migration_name)
            except SkipMigration:
                executed.append(MigrationExecution(name=migration_name, applied=False))
                continue
            applied_names.add(migration_name)
            executed.append(MigrationExecution(name=migration_name, applied=True))

        return MigrationRunSummary(executed=executed)
