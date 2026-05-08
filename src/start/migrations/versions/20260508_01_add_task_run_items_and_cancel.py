from __future__ import annotations

import peewee
from playhouse.migrate import migrate as run_migration

from src.model import BackgroundTaskRun, BackgroundTaskRunItem
from src.start.migrations import SkipMigration


name = "20260508_01_add_task_run_items_and_cancel"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    if not database.table_exists("background_task_run"):
        raise SkipMigration("background_task_run table does not exist")

    with database.bind_ctx([BackgroundTaskRunItem], bind_refs=False, bind_backrefs=False):
        database.create_tables([BackgroundTaskRunItem], safe=True)

    if not _column_exists(database, table_name="background_task_run", column_name="cancel_requested_at"):
        run_migration(migrator.add_column("background_task_run", "cancel_requested_at", peewee.DateTimeField(null=True)))
    if not _column_exists(database, table_name="background_task_run", column_name="cancel_reason"):
        run_migration(migrator.add_column("background_task_run", "cancel_reason", peewee.TextField(null=True)))
