"""启动期表维护：autovacuum 参数与删列后的一次性空间回收。

由 ``commands.migrate`` 在迁移和建表之后调用，容器启动链与裸机 CLI 都会经过，
不依赖任何手工维护命令。
"""

from __future__ import annotations

from loguru import logger

from src.model import SchemaMigration
from src.start.migrations.runner import (
    DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME,
    DROP_MOVIE_EXTRA_MIGRATION_NAME,
)

# 高频更新表的 autovacuum 阈值：默认 20% 在大表上要攒几十万死元组才会清理，
# 可见性映射长期不新鲜，index-only scan 与反连接都要回堆取可见性。
_AUTOVACUUM_SETTINGS = {
    "movie": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        "autovacuum_vacuum_threshold": "500",
    },
    "media_thumbnail": {
        "autovacuum_vacuum_scale_factor": "0.02",
        "autovacuum_analyze_scale_factor": "0.01",
        "autovacuum_vacuum_threshold": "500",
    },
}

# 一次性完成的维护动作记在 schema_migration 台账里，以迁移名加后缀命名，避免与迁移名冲突。
_DROP_COLUMN_COMPACTIONS = (
    (DROP_MOVIE_EXTRA_MIGRATION_NAME, "movie"),
    (DROP_IMAGE_DERIVED_SIZES_MIGRATION_NAME, "image"),
)


def run_startup_maintenance(database) -> None:
    """迁移/建表之后的维护入口；单步失败只告警，不阻断服务启动。"""
    for step in (ensure_autovacuum_settings, compact_dropped_column_tables):
        try:
            step(database)
        except Exception as exc:
            logger.warning("Startup maintenance step {} failed: {}", step.__name__, exc)


def ensure_autovacuum_settings(database) -> None:
    """按需写入 autovacuum 存储参数；已一致时不产生写入。"""
    available_tables = set(database.get_tables())
    for table_name, desired in _AUTOVACUUM_SETTINGS.items():
        if table_name not in available_tables:
            continue
        current = _current_reloptions(database, table_name)
        missing = {
            key: value
            for key, value in desired.items()
            if current.get(key) != value
        }
        if not missing:
            continue
        assignments = ", ".join(f"{key} = {value}" for key, value in missing.items())
        database.execute_sql(f'ALTER TABLE "{table_name}" SET ({assignments})')
        logger.info(
            "Applied autovacuum settings table={} options={}",
            table_name,
            missing,
        )


def compact_dropped_column_tables(database) -> None:
    """删列后的一次性表重写，逐表回收 dropped 属性占用的空间。

    ``VACUUM FULL`` 重写后 dropped 属性条目仍保留在系统目录里，无法据此判断是否已重写，
    因此用 ``schema_migration`` 台账记录每张表一次性的完成标记；失败时不落标记，下次启动重试。
    """
    for migration_name, table_name in _DROP_COLUMN_COMPACTIONS:
        if not _ledger_entry_exists(database, migration_name):
            continue
        marker = f"{migration_name}:compacted"
        if _ledger_entry_exists(database, marker):
            continue
        logger.info(
            "Compacting {} table to reclaim dropped column storage; "
            "this may take a few minutes on large libraries",
            table_name,
        )
        try:
            database.execute_sql(f"VACUUM (FULL, ANALYZE) {table_name}")
        except Exception as exc:
            logger.warning(
                "{} table compaction failed; will retry on next startup: {}",
                table_name,
                exc,
            )
            continue
        _record_ledger_entry(database, marker)
        logger.info("{} table compaction finished", table_name)


def _current_reloptions(database, table_name: str) -> dict[str, str]:
    row = database.execute_sql(
        "SELECT reloptions FROM pg_class WHERE oid = %s::regclass", (table_name,)
    ).fetchone()
    if not row or not row[0]:
        return {}
    options: dict[str, str] = {}
    for item in row[0]:
        key, _, value = item.partition("=")
        options[key] = value
    return options


def _ledger_entry_exists(database, entry_name: str) -> bool:
    # 与迁移 runner 相同：记录表查询必须绑定到目标数据库。
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return (
            SchemaMigration.select()
            .where(SchemaMigration.name == entry_name)
            .exists()
        )


def _record_ledger_entry(database, entry_name: str) -> None:
    with database.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.create(name=entry_name)
