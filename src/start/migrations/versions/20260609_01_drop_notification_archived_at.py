from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration


name = "20260609_01_drop_notification_archived_at"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _drop_archived_at_indexes(database, migrator) -> None:
    for index in database.get_indexes("system_notification"):
        if "archived_at" not in getattr(index, "columns", []):
            continue
        # archived_at 历史上带 index=True，删列前必须先移除引用该列的索引。
        run_migration(migrator.drop_index("system_notification", index.name))


def migrate(database, migrator) -> None:
    if not database.table_exists("system_notification"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要删列。
        raise SkipMigration("system_notification table does not exist")

    if not _column_exists(database, table_name="system_notification", column_name="archived_at"):
        # 新库按当前模型建表后本就没有该列，幂等返回。
        return

    _drop_archived_at_indexes(database, migrator)
    run_migration(migrator.drop_column("system_notification", "archived_at"))
