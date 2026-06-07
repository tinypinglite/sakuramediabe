from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260607_01_add_import_job_transfer_mode"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    if not database.table_exists("import_job"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要补列。
        raise SkipMigration("import_job table does not exist")

    if _column_exists(database, table_name="import_job", column_name="transfer_mode"):
        return

    # 默认值通过 Peewee 字段定义交由 migrator 渲染，兼容 SQLite/PostgreSQL，不手写字面量。
    run_migration(
        migrator.add_column(
            "import_job",
            "transfer_mode",
            peewee.CharField(max_length=32, default="auto"),
        )
    )
