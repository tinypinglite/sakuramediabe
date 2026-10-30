"""Add a reusable GCID cache to media."""

from __future__ import annotations

name = "20261101_01_add_media_content_gcid"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE media ADD COLUMN IF NOT EXISTS content_gcid VARCHAR(40) NULL"
    )
