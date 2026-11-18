"""新增浏览记录表。

影片/女优详情页打开时上报；同一实体唯一，重复浏览只刷新 viewed_at。
保留最近 N 条由服务层裁剪。存量库由本迁移建出，新库由 create_tables 直接建表。
"""

name = "20261010_01_add_view_history"


def migrate(database) -> None:
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS view_history (
            id SERIAL PRIMARY KEY,
            entity_type VARCHAR(16) NOT NULL,
            entity_id INTEGER NOT NULL,
            viewed_at TIMESTAMP NOT NULL,
            UNIQUE (entity_type, entity_id)
        )
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS viewhistory_viewed_at
        ON view_history (viewed_at)
    """)
