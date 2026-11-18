"""Add the reverse lookup index for media tag filtering."""

name = "20261031_03_add_media_tag_reverse_index"


def migrate(database) -> None:
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS media_media_tag_tag_id_media_id "
        "ON media_media_tag (tag_id, media_id)"
    )
