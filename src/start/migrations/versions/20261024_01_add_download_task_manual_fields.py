"""新增手动提交下载任务的导入元数据。

media_kind / collection_id / delete_source_after_import 决定下载完成后的导入路径；
display_name 保存用户提交名，避免被 provider 同步回来的任务名覆盖。
新库由 create_tables 直接建列。
"""

name = "20261024_01_add_download_task_manual_fields"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE download_task ADD COLUMN IF NOT EXISTS media_kind VARCHAR(32) NOT NULL DEFAULT 'jav'"
    )
    database.execute_sql(
        "ALTER TABLE download_task ADD COLUMN IF NOT EXISTS collection_id INTEGER NULL"
    )
    database.execute_sql(
        "ALTER TABLE download_task ADD COLUMN IF NOT EXISTS delete_source_after_import BOOLEAN NOT NULL DEFAULT FALSE"
    )
    database.execute_sql(
        "ALTER TABLE download_task ADD COLUMN IF NOT EXISTS display_name VARCHAR(255) NULL"
    )
