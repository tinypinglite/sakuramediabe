"""缩略图按文件指纹归档：Media 删除后保留产物，同 hash 再次入库时自动收养。

存量库由本迁移建出，新库由 create_tables 直接建表。
"""

name = "20261014_01_add_media_thumbnail_archive"


def migrate(database) -> None:
    database.execute_sql("""
        ALTER TABLE media
            ADD COLUMN IF NOT EXISTS thumbnail_adoption_blocked BOOLEAN NOT NULL DEFAULT FALSE
    """)
    database.execute_sql("""
        CREATE TABLE IF NOT EXISTS media_thumbnail_archive (
            id SERIAL PRIMARY KEY,
            file_hash VARCHAR(64) NOT NULL,
            image_id INTEGER NOT NULL REFERENCES image(id) ON DELETE RESTRICT,
            "offset" INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS mediathumbnailarchive_file_hash
        ON media_thumbnail_archive (file_hash)
    """)
    database.execute_sql("""
        CREATE INDEX IF NOT EXISTS mediathumbnailarchive_offset
        ON media_thumbnail_archive ("offset")
    """)
    database.execute_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS mediathumbnailarchive_file_hash_offset
        ON media_thumbnail_archive (file_hash, "offset")
    """)
