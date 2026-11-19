"""Add the saved filter snapshot for smart playlists."""

from __future__ import annotations

name = "20261120_01_add_playlist_smart_filter"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE playlist ADD COLUMN IF NOT EXISTS filter_json TEXT NULL"
    )
