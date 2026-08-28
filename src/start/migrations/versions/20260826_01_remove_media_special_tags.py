"""移除已废弃的媒体特殊标签及其虚拟系统列表。"""

from __future__ import annotations

name = "20260826_01_remove_media_special_tags"


def migrate(database) -> None:
    database.execute_sql(
        "DELETE FROM playlist_movie "
        "WHERE playlist_id IN (SELECT id FROM playlist WHERE kind IN ('vr', '4k'))"
    )
    database.execute_sql("DELETE FROM playlist WHERE kind IN ('vr', '4k')")
    database.execute_sql("ALTER TABLE media DROP COLUMN IF EXISTS special_tags")
