from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260730_03_drop_movie_subtitle_fetch_states"


def migrate(database, migrator) -> None:
    """删除 ``movie_subtitle_fetch`` 遗留状态行。

    该 task_key 不在新内核注册的任务表里，功能代码已无任何生产/消费点，状态行只是
    旧字幕识别链路留下的孤儿数据（生产约 1.2w 行），新内核永远不会再读到它们。
    """
    if not database.table_exists("resource_task_state"):
        raise SkipMigration("resource_task_state table does not exist")
    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key = %s",
        ("movie_subtitle_fetch",),
    )
