from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260714_04_normalize_cloud115_transfer_mode"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    if not database.table_exists("import_job"):
        raise SkipMigration("import_job table does not exist")
    required = {"source_cid", "transfer_mode"}
    if not all(
        _column_exists(database, table_name="import_job", column_name=column)
        for column in required
    ):
        raise SkipMigration("import_job cloud115 columns do not exist")
    database.execute_sql(
        "UPDATE import_job SET transfer_mode = %s "
        "WHERE source_cid IS NOT NULL AND transfer_mode = %s",
        ("cleanup-source", "move"),
    )
