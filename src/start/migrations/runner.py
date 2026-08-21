from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType

from peewee import Database, PostgresqlDatabase
from playhouse.migrate import PostgresqlMigrator

from src.model import SchemaMigration

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

# 0.5.0 只承接 v0.4.21 的最后一条迁移记录；旧版本文件会随本次发布移除。
SUPPORTED_BASE_MIGRATION_NAME = "20260816_01_add_movie_field_owners"
CONSOLIDATED_MIGRATION_NAME = "20260821_01_consolidate_task_runtime"


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


def _has_columns(database: Database, table_name: str, column_names: set[str]) -> bool:
    if not database.table_exists(table_name):
        return False
    existing_columns = {column.name for column in database.get_columns(table_name)}
    return column_names <= existing_columns


def _is_current_schema(database: Database) -> bool:
    """识别已按 0.5.0 模型建好的新库，允许其首次记录收敛迁移。"""
    return all(
        _has_columns(database, table_name, column_names)
        for table_name, column_names in {
            "movie": {"interaction_synced_at"},
            "media": {"thumbnail_generation_state"},
            "download_task": {"raw_state", "import_task_run_id"},
            "system_notification": {"dedupe_key"},
        }.items()
    )


def _is_empty_schema(database: Database) -> bool:
    # run_pending_migrations 会先创建审计表；除此之外没有业务表才算全新数据库。
    return set(database.get_tables()) <= {"schema_migration"}


def _validate_migration_source(database: Database, applied_names: set[str]) -> None:
    if CONSOLIDATED_MIGRATION_NAME in applied_names:
        return
    if SUPPORTED_BASE_MIGRATION_NAME in applied_names:
        return
    if not applied_names and (_is_empty_schema(database) or _is_current_schema(database)):
        return
    raise ValueError(
        "unsupported_migration_source: v0.5.0 only supports upgrading from v0.4.21; "
        "fresh databases are also supported"
    )


def run_pending_migrations(database: Database) -> MigrationRunSummary:
    # 迁移记录表的查询和写入必须绑定到目标数据库，避免被全局 proxy 残留状态污染。
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        # 迁移记录表由迁移命令显式托管，不依赖 initdb/aps 启动期补库。
        database.create_tables([SchemaMigration], safe=True)
        migrator = _build_migrator(database)
        applied_names = {item.name for item in SchemaMigration.select(SchemaMigration.name)}
        _validate_migration_source(database, applied_names)
        executed: list[MigrationExecution] = []

        for module in _list_migration_modules():
            migration_name = str(getattr(module, "name", "")).strip()
            migrate_callable = getattr(module, "migrate", None)
            if not migration_name:
                raise ValueError(f"migration_name_missing: {module.__name__}")
            if not callable(migrate_callable):
                raise TypeError(f"migration_callable_missing: {migration_name}")
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
