from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260806_01_add_download_task_started_at"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    """为 download_task 补 download_started_at 列。

    qB 停滞/慢速任务清理按"进入活跃下载态的时刻"计时（排队时间不计），
    qB 接口无此字段，由对账侧维护本列。
    """
    if not database.table_exists("download_task"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要补列。
        raise SkipMigration("download_task table does not exist")

    if _column_exists(database, table_name="download_task", column_name="download_started_at"):
        return

    # 列定义经由 Peewee 字段渲染，不手写 SQL 字面量。
    run_migration(
        migrator.add_column(
            "download_task",
            "download_started_at",
            peewee.DateTimeField(null=True),
        )
    )
