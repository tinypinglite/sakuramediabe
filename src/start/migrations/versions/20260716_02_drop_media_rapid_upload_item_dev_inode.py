from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260716_02_drop_media_rapid_upload_item_dev_inode"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # 弃用 source_device / source_inode：mergerfs 等 FUSE 挂载会给出 > 2^63-1 的
    # inode 直接把 INSERT 打到 bigint out of range；这两个字段本来只用于 size+mtime
    # 之外补一层原子替换检测，收益极小且和 hash-inode 语义冲突，一并去掉。
    if not database.table_exists("media_rapid_upload_item"):
        raise SkipMigration("media_rapid_upload_item table does not exist")
    operations = []
    if _column_exists(database, table_name="media_rapid_upload_item", column_name="source_device"):
        operations.append(migrator.drop_column("media_rapid_upload_item", "source_device"))
    if _column_exists(database, table_name="media_rapid_upload_item", column_name="source_inode"):
        operations.append(migrator.drop_column("media_rapid_upload_item", "source_inode"))
    if not operations:
        return
    run_migration(*operations)
