"""Add reusable full-file content hashes to media."""

from __future__ import annotations

name = "20261031_05_add_media_content_hashes"


def migrate(database) -> None:
    for column_name, column_type in (
        ("content_md5", "VARCHAR(32)"),
        ("content_sha1", "VARCHAR(40)"),
    ):
        database.execute_sql(
            f"ALTER TABLE media ADD COLUMN IF NOT EXISTS {column_name} {column_type} NULL"
        )
