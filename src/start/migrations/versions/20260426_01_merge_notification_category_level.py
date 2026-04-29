from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration


name = "20260426_01_merge_notification_category_level"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _drop_level_indexes(database, migrator) -> None:
    for index in database.get_indexes("system_notification"):
        if "level" not in getattr(index, "columns", []):
            continue
        # Peewee 历史版本的 index=True 自动索引名是 systemnotification_level，
        # 这里按索引列识别，避免只兼容手写 SQL 里的某一种命名。
        run_migration(migrator.drop_index("system_notification", index.name))


def _backfill_categories(database) -> None:
    # 严重等级优先：旧 level=error/warning 直接转为新 category，
    # 其余按旧 category 是否为 reminder 兜底，确保历史脏数据也有确定输出。
    database.execute_sql(
        "UPDATE system_notification SET category = 'error' WHERE level = 'error'"
    )
    database.execute_sql(
        "UPDATE system_notification SET category = 'warning' "
        "WHERE level = 'warning' AND category <> 'error'"
    )
    database.execute_sql(
        "UPDATE system_notification SET category = 'info' "
        "WHERE category NOT IN ('reminder', 'info', 'warning', 'error')"
    )


def migrate(database, migrator) -> None:
    if not database.table_exists("system_notification"):
        # 目标表尚未建出时不能误记迁移完成，留待后续建表后再判定是否需要合并字段。
        raise SkipMigration("system_notification table does not exist")

    if not _column_exists(database, table_name="system_notification", column_name="level"):
        return

    _backfill_categories(database)

    # SQLite 删除列前要求先移除引用该列的旧索引，否则 DROP COLUMN 会失败。
    _drop_level_indexes(database, migrator)

    run_migration(migrator.drop_column("system_notification", "level"))
