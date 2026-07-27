from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260728_01_add_movie_number_upper_index"

# 与 src/model/catalog/movies.py 里 Movie.add_index 声明的函数索引同名：
# 新库由 initdb 的 create_tables 建出，存量库靠本迁移补齐。
_INDEX_NAME = "movie_movie_number_upper"


def migrate(database, migrator) -> None:
    # 人工输入按番号点查统一走 UPPER(movie_number) 等值匹配（service_helpers.find_movie_by_number），
    # 该函数索引保证这条路径不退化为顺扫。movie_number 列本身存 provider 规范原样，不做改写。
    if not database.table_exists("movie"):
        raise SkipMigration("movie table does not exist")

    database.execute_sql(
        f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} ON movie (UPPER(movie_number))"
    )
