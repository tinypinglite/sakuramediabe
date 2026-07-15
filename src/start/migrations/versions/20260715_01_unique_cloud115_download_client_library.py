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
    # 先在应用层检测重复，避免 CREATE UNIQUE INDEX 因存量脏数据直接抛不带
    # library_id 的原生 Postgres 报错。参考同批次 20260714_03 的防御模式：
    # 用语义化 ValueError 明确告知涉及的 library_ids，便于部署时人工介入。
    cursor = database.execute_sql(
        """
        SELECT media_library_id
        FROM download_client
        WHERE kind = 'cloud115' AND media_library_id IS NOT NULL
        GROUP BY media_library_id
        HAVING COUNT(*) > 1
        """
    )
    duplicated_library_ids = [row[0] for row in cursor.fetchall()]
    if duplicated_library_ids:
        raise ValueError(
            "cloud115_download_client_library_duplicate: "
            f"library_ids={duplicated_library_ids}. "
            "manually keep only one cloud115 download client per media library, "
            "then re-run migrate."
        )
    database.execute_sql(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON download_client (media_library_id)
        WHERE kind = 'cloud115'
        """
    )
