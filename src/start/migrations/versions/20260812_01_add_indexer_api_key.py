from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260812_01_add_indexer_api_key"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    """为 indexer 补 api_key 列：每个索引器独立可选的 Torznab 鉴权 key。

    不做存量回填：旧全局 key 在 config.toml，升级后由用户在 /indexer-settings 逐个重配。
    """
    if not database.table_exists("indexer"):
        raise SkipMigration("indexer table does not exist")

    if _column_exists(database, table_name="indexer", column_name="api_key"):
        return

    # 列定义经由 Peewee 字段渲染，不手写 SQL 字面量。
    run_migration(
        migrator.add_column(
            "indexer",
            "api_key",
            peewee.CharField(max_length=255, null=True),
        )
    )
