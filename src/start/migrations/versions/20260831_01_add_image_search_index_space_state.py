"""记录图搜索索引对应的嵌入空间。"""

from __future__ import annotations

name = "20260831_01_add_image_search_index_space_state"


def migrate(database) -> None:
    database.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS image_search_index_state (
            id INTEGER PRIMARY KEY,
            indexed_space_id VARCHAR(255) NOT NULL
        )
        """
    )
