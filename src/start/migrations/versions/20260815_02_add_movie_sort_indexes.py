from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260815_02_add_movie_sort_indexes"

# 与 src/model/catalog/movies.py 里 Movie.add_index 声明的排序复合索引同名：
# 新库由 initdb 的 create_tables 建出，存量库靠本迁移补齐。
_INDEX_SQLS = (
    (
        "movie_release_date_sort",
        "CREATE INDEX IF NOT EXISTS movie_release_date_sort"
        " ON movie (release_date DESC NULLS LAST, id DESC NULLS LAST)",
    ),
    (
        "movie_subscribed_at_sort",
        "CREATE INDEX IF NOT EXISTS movie_subscribed_at_sort"
        " ON movie (subscribed_at DESC NULLS LAST, id DESC NULLS LAST)",
    ),
)


def migrate(database, migrator) -> None:
    # 影片列表按 release_date / subscribed_at 排序时走 NULLS LAST 表达（build_ordered_expressions），
    # 同名复合索引保证排序直接命中索引：旧实现用 (col IS NULL) 垫后，planner 无法用单列索引
    # 服务排序，30w 行列表页实测全表扫 + top-N heapsort 约 200ms+，换成 Index Scan 后约 4ms。
    if not database.table_exists("movie"):
        raise SkipMigration("movie table does not exist")

    for _index_name, _sql in _INDEX_SQLS:
        database.execute_sql(_sql)
