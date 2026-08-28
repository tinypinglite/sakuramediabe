from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import ModuleType

from peewee import Database

from src.model import SchemaMigration

VERSIONS_DIR = Path(__file__).resolve().parent / "versions"

# 当前 provider 版只承接精确 v0.5.3；更老版本必须先升级到 v0.5.3。
SUPPORTED_BASE_MIGRATION_NAME = "20260816_01_add_movie_field_owners"
CONSOLIDATED_MIGRATION_NAME = "20260821_01_consolidate_task_runtime"
MOVIE_COLLECTION_OWNER_MIGRATION_NAME = "20260823_01_unify_movie_collection_owner"
ACTOR_GENDER_BACKFILL_MIGRATION_NAME = "20260823_02_backfill_actor_gender_from_movie_extra"
MOVIE_BLACKLIST_MIGRATION_NAME = "20260823_03_add_movie_blacklist"
MEDIA_SPECIAL_TAGS_REMOVAL_MIGRATION_NAME = "20260826_01_remove_media_special_tags"
IMAGE_SEARCH_QUEUE_INDEXES_MIGRATION_NAME = "20260830_01_add_image_search_queue_indexes"
IMAGE_SEARCH_INDEX_SPACE_STATE_MIGRATION_NAME = "20260831_01_add_image_search_index_space_state"


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


def _load_migration_module(path: Path) -> ModuleType:
    return import_module(f"src.start.migrations.versions.{path.stem}")


def _list_migration_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modules.append(_load_migration_module(path))
    return modules


def _is_empty_schema(database: Database) -> bool:
    # run_pending_migrations 会先创建审计表；除此之外没有业务表才算全新数据库。
    return set(database.get_tables()) <= {"schema_migration"}


def _validate_migration_source(database: Database, applied_names: set[str]) -> None:
    from src.start.legacy_v053_upgrade import classify_database_schema

    if classify_database_schema(database) == "legacy_v053":
        raise ValueError(
            "legacy_v053_upgrade_required: run the dedicated upgrade-v053 command first"
        )
    if CONSOLIDATED_MIGRATION_NAME in applied_names:
        return
    if not applied_names and _is_empty_schema(database):
        return
    raise ValueError(
        "unsupported_migration_source: this release only supports the exact v0.5.3 "
        "database or a fresh database"
    )


def run_pending_migrations(database: Database) -> MigrationRunSummary:
    # 迁移记录表的查询和写入必须绑定到目标数据库，避免被全局 proxy 残留状态污染。
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        # 迁移记录表由迁移命令显式托管，不依赖 initdb/aps 启动期补库。
        database.create_tables([SchemaMigration], safe=True)
        applied_names = {item.name for item in SchemaMigration.select(SchemaMigration.name)}
        _validate_migration_source(database, applied_names)
        executed: list[MigrationExecution] = []

        if _is_empty_schema(database):
            if CONSOLIDATED_MIGRATION_NAME not in applied_names:
                migration_name = CONSOLIDATED_MIGRATION_NAME
                with database.atomic():
                    SchemaMigration.create(name=migration_name)
                return MigrationRunSummary(
                    executed=[MigrationExecution(name=migration_name, applied=True)]
                )
            # 新库只记录 consolidated marker，业务迁移由当前模型建表，不应尝试执行。
            return MigrationRunSummary(executed=[])

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

            with database.atomic():
                migrate_callable(database)
                SchemaMigration.create(name=migration_name)
            applied_names.add(migration_name)
            executed.append(MigrationExecution(name=migration_name, applied=True))

        return MigrationRunSummary(executed=executed)
