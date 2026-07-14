from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260715_01_unique_cloud115_download_client_library"
INDEX_NAME = "download_client_cloud115_library_unique"


def migrate(database, migrator) -> None:
    """同一 115 媒体库只允许存在一个 Cloud115 下载入口。"""
    if not database.table_exists("download_client"):
        raise SkipMigration("download_client table does not exist")
    columns = {column.name for column in database.get_columns("download_client")}
    if not {"kind", "media_library_id"}.issubset(columns):
        raise SkipMigration("download_client cloud115 columns do not exist")
    database.execute_sql(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON download_client (media_library_id)
        WHERE kind = 'cloud115'
        """
    )
