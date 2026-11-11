"""补齐影片互动数排序的复合索引。"""

from __future__ import annotations

name = "20261019_01_add_movie_interaction_sort_indexes"

# 与 src/model/catalog/movies.py 的 Movie.add_index 同名：新库由建表创建，存量库靠本迁移补齐。
_INDEX_SQLS = (
    (
        "movie_heat_sort",
        "heat",
        "CREATE INDEX IF NOT EXISTS movie_heat_sort ON movie (heat DESC, id DESC)",
    ),
    (
        "movie_score_sort",
        "score",
        "CREATE INDEX IF NOT EXISTS movie_score_sort ON movie (score DESC, id DESC)",
    ),
    (
        "movie_score_number_sort",
        "score_number",
        (
            "CREATE INDEX IF NOT EXISTS movie_score_number_sort "
            "ON movie (score_number DESC, id DESC)"
        ),
    ),
    (
        "movie_watched_count_sort",
        "watched_count",
        (
            "CREATE INDEX IF NOT EXISTS movie_watched_count_sort "
            "ON movie (watched_count DESC, id DESC)"
        ),
    ),
    (
        "movie_want_watch_count_sort",
        "want_watch_count",
        (
            "CREATE INDEX IF NOT EXISTS movie_want_watch_count_sort "
            "ON movie (want_watch_count DESC, id DESC)"
        ),
    ),
    (
        "movie_comment_count_sort",
        "comment_count",
        (
            "CREATE INDEX IF NOT EXISTS movie_comment_count_sort "
            "ON movie (comment_count DESC, id DESC)"
        ),
    ),
)


def migrate(database) -> None:
    columns = {column.name for column in database.get_columns("movie")}
    for _index_name, column_name, sql in _INDEX_SQLS:
        if column_name not in columns:
            continue
        database.execute_sql(sql)
    # 复合索引 (score DESC, id DESC) 已覆盖评分过滤和排序，旧的单列索引不再需要。
    if "score" in columns:
        database.execute_sql("DROP INDEX IF EXISTS movie_score")
