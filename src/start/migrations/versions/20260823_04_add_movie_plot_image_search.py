"""为剧情图以图搜图增加索引状态。"""

from __future__ import annotations

name = "20260823_04_add_movie_plot_image_search"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE movie_plot_image ADD COLUMN IF NOT EXISTS joytag_index_status INTEGER NOT NULL DEFAULT 0"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movieplotimage_joytag_index_status "
        "ON movie_plot_image (joytag_index_status)"
    )
