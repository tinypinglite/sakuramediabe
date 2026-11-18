"""标记用户删除最后一份 Media 后等待重新补库的订阅影片。"""

name = "20261014_02_add_movie_media_replacement"


def migrate(database) -> None:
    database.execute_sql("""
        ALTER TABLE movie
            ADD COLUMN IF NOT EXISTS media_replacement_pending BOOLEAN NOT NULL DEFAULT FALSE
    """)
