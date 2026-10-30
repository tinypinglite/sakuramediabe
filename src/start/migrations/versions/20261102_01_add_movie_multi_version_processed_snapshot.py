"""Add the processed-media snapshot for multi-version movie groups."""

from __future__ import annotations

name = "20261102_01_add_movie_multi_version_processed_snapshot"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE movie ADD COLUMN IF NOT EXISTS "
        "multi_version_processed_snapshot TEXT NULL"
    )
