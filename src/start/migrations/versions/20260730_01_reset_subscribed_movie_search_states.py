from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260730_01_reset_subscribed_movie_search_states"


def migrate(database, migrator) -> None:
    """任务架构 Wave 2：订阅资源查询迁 kernel，task_key 与 job 合并（决策 #11）。

    旧 key ``subscribed_movie_search`` 的状态行整体清空——新 key
    ``subscribed_movie_auto_download`` 从零重建。用户可见后果（已确认接受）：
    已放弃（exhausted）的订阅复活，各按新预算再查一轮后重新耗尽。
    """
    if not database.table_exists("resource_task_state"):
        raise SkipMigration("resource_task_state table does not exist")
    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key = %s",
        ("subscribed_movie_search",),
    )
