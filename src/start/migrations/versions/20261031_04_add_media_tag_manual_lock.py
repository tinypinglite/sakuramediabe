"""Add the manual assignment lock for media file tags."""

name = "20261031_04_add_media_tag_manual_lock"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE media ADD COLUMN IF NOT EXISTS "
        "is_media_tags_manually_managed BOOLEAN NOT NULL DEFAULT FALSE"
    )
