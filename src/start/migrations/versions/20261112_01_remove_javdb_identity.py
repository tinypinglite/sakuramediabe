"""Remove JavDB-specific identity and host sync fields."""

from __future__ import annotations

name = "20261112_01_remove_javdb_identity"


def migrate(database) -> None:
    database.execute_sql(
        """
        UPDATE actor
        SET field_owners = (
            SELECT COALESCE(
                jsonb_object_agg(
                    key,
                    CASE WHEN value = 'host:javdb' THEN 'host:metadata' ELSE value END
                ),
                '{}'::jsonb
            )
            FROM jsonb_each_text(field_owners)
        )
        WHERE field_owners::text LIKE %s
        """
        ,
        ("%host:javdb%",),
    )
    for column in (
        "javdb_id",
        "javdb_type",
        "gender_checked_at",
        "gender_next_check_at",
        "subscribed_movies_synced_at",
        "subscribed_movies_full_synced_at",
    ):
        database.execute_sql(
            f"ALTER TABLE actor DROP COLUMN IF EXISTS {column}"
        )
    for column in (
        "javdb_id",
        "javdb_next_check_at",
        "interaction_synced_at",
    ):
        database.execute_sql(
            f"ALTER TABLE movie DROP COLUMN IF EXISTS {column}"
        )
