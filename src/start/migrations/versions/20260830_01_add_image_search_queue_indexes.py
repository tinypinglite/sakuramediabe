"""为图像索引待处理队列增加状态与主键复合索引。"""

from __future__ import annotations

name = "20260830_01_add_image_search_queue_indexes"


def migrate(database) -> None:
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS mediathumbnail_image_search_index_status_id "
        "ON media_thumbnail (image_search_index_status, id)"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movieplotimage_image_search_index_status_id "
        "ON movie_plot_image (image_search_index_status, id)"
    )
    database.execute_sql(
        "DROP INDEX IF EXISTS mediathumbnail_image_search_index_status"
    )
    database.execute_sql(
        "DROP INDEX IF EXISTS movieplotimage_image_search_index_status"
    )
