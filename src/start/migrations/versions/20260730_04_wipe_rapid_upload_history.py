from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260730_04_wipe_rapid_upload_history"


def migrate(database, migrator) -> None:
    """清空秒传批次与明细的历史台账。

    秒传批次是纯执行记录，新内核接管并发道后从零起点重建，不做旧数据兼容展示。
    """
    required_tables = {"media_rapid_upload_batch", "media_rapid_upload_item"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    # item.batch_id 是 ON DELETE CASCADE，显式先清子表，避免父表逐行触发级联。
    database.execute_sql("DELETE FROM media_rapid_upload_item")
    database.execute_sql("DELETE FROM media_rapid_upload_batch")
