from __future__ import annotations

from src.model import MediaRapidUploadBatch, MediaRapidUploadItem
from src.start.migrations import SkipMigration

name = "20260716_01_add_media_rapid_upload_jobs"


def migrate(database, migrator) -> None:
    """新增批量媒体秒传批次与逐项恢复记录。"""
    required_tables = {
        "media",
        "media_library",
        "background_task_run",
        "system_notification",
    }
    missing_tables = sorted(
        table_name
        for table_name in required_tables
        if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")
    database.create_tables(
        [MediaRapidUploadBatch, MediaRapidUploadItem],
        safe=True,
    )
