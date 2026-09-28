"""为 actor 增加合并墓碑指针 merged_into_id。"""

from __future__ import annotations

name = "20260929_01_add_actor_merged_into"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE actor ADD COLUMN IF NOT EXISTS merged_into_id INTEGER "
        "REFERENCES actor(id) ON DELETE SET NULL"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS actor_merged_into_id ON actor (merged_into_id)"
    )
