"""新增个人评分维度与影片评分。

评分值以 JSONB 存维度 id -> 分值；总分在写入时算好，供影片列表排序。
存量库由本迁移建表，新库由 create_tables 直接建表。
"""

name = "20261020_01_add_movie_ratings"


def migrate(database) -> None:
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS rating_dimension (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(64) NOT NULL UNIQUE,
            min_value INTEGER NOT NULL DEFAULT 1,
            max_value INTEGER NOT NULL DEFAULT 5,
            weight DOUBLE PRECISION NOT NULL DEFAULT 1
        )
    """)
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS movie_rating (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            movie_id INTEGER NOT NULL UNIQUE REFERENCES movie(id) ON DELETE CASCADE,
            "values" JSONB NOT NULL DEFAULT '{}'::jsonb,
            total_score DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)
