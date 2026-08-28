"""Guard the ordinary migration runner against an unconverted v0.5.3 database."""

from __future__ import annotations

from src.start.legacy_v053_upgrade import classify_database_schema

name = "20260825_01_upgrade_v053_provider_storage"


def migrate(database) -> None:
    if classify_database_schema(database) == "legacy_v053":
        raise ValueError(
            "legacy_v053_upgrade_required: run the dedicated upgrade-v053 command first"
        )
