"""允许插件影片暂缺 JavDB ID，保留首次来源与固定间隔补录时间。"""

name = "20260905_02_add_plugin_movie_metadata"


def migrate(database) -> None:
    database.execute_sql("ALTER TABLE movie ALTER COLUMN javdb_id DROP NOT NULL")
    database.execute_sql(
        "ALTER TABLE movie ADD COLUMN IF NOT EXISTS metadata_source JSONB NULL, "
        "ADD COLUMN IF NOT EXISTS javdb_next_check_at TIMESTAMP NULL"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS movie_javdb_next_check_at ON movie (javdb_next_check_at)"
    )
