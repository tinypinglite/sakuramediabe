"""时刻独立保留图片与来源快照。"""

name = "20260918_01_preserve_media_points"


def migrate(database) -> None:
    database.execute_sql("""
        ALTER TABLE media_point
            ADD COLUMN IF NOT EXISTS image_id INTEGER REFERENCES image(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS movie_number VARCHAR(64),
            ADD COLUMN IF NOT EXISTS video_item_id INTEGER
    """)
    database.execute_sql("""
        UPDATE media_point AS point
        SET image_id = thumbnail.image_id,
            movie_number = media.movie_number,
            video_item_id = media.video_item_id
        FROM media_thumbnail AS thumbnail, media
        WHERE point.thumbnail_id = thumbnail.id AND point.media_id = media.id
            AND point.image_id IS NULL
    """)
    database.execute_sql("""
        ALTER TABLE media_point
            ALTER COLUMN image_id SET NOT NULL,
            ALTER COLUMN media_id DROP NOT NULL,
            ALTER COLUMN thumbnail_id DROP NOT NULL,
            DROP CONSTRAINT media_point_media_id_fkey,
            DROP CONSTRAINT media_point_thumbnail_id_fkey,
            ADD CONSTRAINT media_point_media_id_fkey
                FOREIGN KEY (media_id) REFERENCES media(id) ON DELETE SET NULL,
            ADD CONSTRAINT media_point_thumbnail_id_fkey
                FOREIGN KEY (thumbnail_id) REFERENCES media_thumbnail(id) ON DELETE SET NULL
    """)
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS mediapoint_image_id ON media_point (image_id)"
    )
