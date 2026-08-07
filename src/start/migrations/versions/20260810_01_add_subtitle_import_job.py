from __future__ import annotations

from src.model import SubtitleImportJob
from src.start.migrations import SkipMigration

name = "20260810_01_add_subtitle_import_job"


def migrate(database, migrator) -> None:
    """新增手动字幕目录导入作业表。"""
    if not database.table_exists("background_task_run"):
        raise SkipMigration("background_task_run table does not exist")
    # safe=True 只补建缺失表；全新 initdb 已建出该表时此处为空操作并正常记为已应用。
    database.create_tables([SubtitleImportJob], safe=True)
