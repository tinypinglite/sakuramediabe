"""Add user-defined media tags and media associations."""

name = "20261031_01_add_media_tags"


def migrate(database) -> None:
    database.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS media_tag (
            id SERIAL PRIMARY KEY,
            name VARCHAR(64) NOT NULL UNIQUE,
            include_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
            exclude_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    database.execute_sql(
        """
        CREATE TABLE IF NOT EXISTS media_media_tag (
            id SERIAL PRIMARY KEY,
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES media_tag(id) ON DELETE CASCADE
        )
        """
    )
    database.execute_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS media_media_tag_media_id_tag_id
        ON media_media_tag (media_id, tag_id)
        """
    )
