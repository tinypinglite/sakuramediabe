from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import VideoImportJob
from src.start.migrations import SkipMigration

name = "20260714_05_add_video_import_job_cloud_source"


def _column_exists(database, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns("video_import_job"))


def migrate(database, migrator) -> None:
    if not database.table_exists("video_import_job"):
        raise SkipMigration("video_import_job table does not exist")

    operations = []
    for column_name in ("source_cid", "source_fid"):
        if not _column_exists(database, column_name):
            operations.append(
                migrator.add_column(
                    "video_import_job",
                    column_name,
                    VideoImportJob._meta.fields[column_name],
                )
            )
    if not operations:
        raise SkipMigration("video_import_job cloud source columns already exist")
    run_migration(*operations)
