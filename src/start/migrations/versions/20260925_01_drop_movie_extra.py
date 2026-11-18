"""删除影片原始详情留档列 movie.extra。"""

from __future__ import annotations

name = "20260925_01_drop_movie_extra"


def migrate(database) -> None:
    # 该列存的是 JavDB 原始报文留档，已无任何读取方，写入点同步移除；
    # 表内空间由启动期一次性 compaction（src/start/maintenance.py）回收。
    database.execute_sql("ALTER TABLE movie DROP COLUMN IF EXISTS extra")
