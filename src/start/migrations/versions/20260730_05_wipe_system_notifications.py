from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260730_05_wipe_system_notifications"


def migrate(database, migrator) -> None:
    """清空系统通知历史。

    存量通知指向的是旧内核的任务运行记录（已在 20260729_02 清空），点进去只剩空壳，
    与活动流一起从零起点重建。
    """
    if not database.table_exists("system_notification"):
        raise SkipMigration("system_notification table does not exist")
    database.execute_sql("DELETE FROM system_notification")
