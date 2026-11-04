"""Add playback structure health to media."""

from __future__ import annotations

name = "20261105_01_add_media_playback_health"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE media ADD COLUMN IF NOT EXISTS playback_health JSONB NULL"
    )
