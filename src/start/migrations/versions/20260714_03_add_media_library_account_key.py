from __future__ import annotations

import json
import re

import peewee
from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260714_03_add_media_library_account_key"

_UID_PATTERN = re.compile(r"(?:^|;\s*)UID=(\d+)_")


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _index_exists(database, *, table_name: str, columns: tuple[str, ...]) -> bool:
    return any(tuple(index.columns) == columns for index in database.get_indexes(table_name))


def migrate(database, migrator) -> None:
    if not database.table_exists("media_library"):
        raise SkipMigration("media_library table does not exist")

    if not _column_exists(
        database, table_name="media_library", column_name="backend_account_key"
    ):
        run_migration(
            migrator.add_column(
                "media_library",
                "backend_account_key",
                peewee.CharField(max_length=255, null=True),
            )
        )

    rows = database.execute_sql(
        "SELECT id, backend_config FROM media_library "
        "WHERE backend = %s AND backend_account_key IS NULL",
        ("cloud115",),
    ).fetchall()
    account_keys: dict[str, int] = {}
    for library_id, raw_config in rows:
        config = raw_config if isinstance(raw_config, dict) else json.loads(raw_config or "{}")
        cookies = str(config.get("cookies") or "")
        match = _UID_PATTERN.search(cookies)
        if match is None:
            raise ValueError(
                f"cloud115_media_library_uid_missing: library_id={library_id}"
            )
        account_key = f"cloud115:{match.group(1)}"
        previous_id = account_keys.get(account_key)
        if previous_id is not None:
            raise ValueError(
                "cloud115_media_library_account_duplicate: "
                f"account_key={account_key} library_ids={previous_id},{library_id}"
            )
        account_keys[account_key] = library_id
        database.execute_sql(
            "UPDATE media_library SET backend_account_key = %s WHERE id = %s",
            (account_key, library_id),
        )

    duplicate = database.execute_sql(
        "SELECT backend_account_key, MIN(id), MAX(id) FROM media_library "
        "WHERE backend_account_key IS NOT NULL GROUP BY backend_account_key "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise ValueError(
            "cloud115_media_library_account_duplicate: "
            f"account_key={duplicate[0]} library_ids={duplicate[1]},{duplicate[2]}"
        )

    if not _index_exists(
        database, table_name="media_library", columns=("backend_account_key",)
    ):
        run_migration(
            migrator.add_index("media_library", ("backend_account_key",), True)
        )
