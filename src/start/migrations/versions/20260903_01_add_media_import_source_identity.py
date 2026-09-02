"""保存可选 Provider 导入源识别能力返回的标识。"""

from __future__ import annotations

name = "20260903_01_add_media_import_source_identity"


def migrate(database) -> None:
    database.execute_sql(
        "ALTER TABLE media ADD COLUMN IF NOT EXISTS import_source_identity VARCHAR(512) NULL"
    )
    database.execute_sql(
        "CREATE INDEX IF NOT EXISTS media_library_id_import_source_identity "
        "ON media (library_id, import_source_identity)"
    )
