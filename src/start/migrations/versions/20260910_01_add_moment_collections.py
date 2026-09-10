"""新增时刻合集及有序成员关系。"""

name = "20260910_01_add_moment_collections"


def migrate(database) -> None:
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS moment_collection (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            name VARCHAR(255) NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS moment_collection_item (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            collection_id INTEGER NOT NULL REFERENCES moment_collection(id) ON DELETE CASCADE,
            point_id INTEGER NOT NULL REFERENCES media_point(id) ON DELETE CASCADE,
            position INTEGER NOT NULL
        )
    """)
    database.execute_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS momentcollectionitem_collection_id_point_id
        ON moment_collection_item (collection_id, point_id)
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS momentcollectionitem_position
        ON moment_collection_item (position)
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS momentcollectionitem_point_id
        ON moment_collection_item (point_id)
    """)
