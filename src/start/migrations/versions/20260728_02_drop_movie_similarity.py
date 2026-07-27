"""删除已由 Qdrant 稀疏索引替代的影片相似度结果表。"""

name = "20260728_02_drop_movie_similarity"


def migrate(database, migrator) -> None:
    del migrator
    if database.table_exists("movie_similarity"):
        database.execute_sql('DROP TABLE "movie_similarity"')
