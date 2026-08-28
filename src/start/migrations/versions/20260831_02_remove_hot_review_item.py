"""移除已废弃的热评快照表。"""

from __future__ import annotations

name = "20260831_02_remove_hot_review_item"


def migrate(database) -> None:
    database.execute_sql("DROP TABLE IF EXISTS hot_review_item")
