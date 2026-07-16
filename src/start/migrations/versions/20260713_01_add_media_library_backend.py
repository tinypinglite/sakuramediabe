from __future__ import annotations

import json

from playhouse.migrate import migrate as run_migration

from src.model import MediaLibrary
from src.model.base import JsonTextField
from src.start.migrations import SkipMigration

name = "20260713_01_add_media_library_backend"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    # media_library 表迁到 backend + backend_config 抽象：新增两列 → 用旧 root_path 回填 → 删列。
    # 新装用户 initdb 已直接建出新 schema，本迁移逐步 idempotent，命中已完成态时全部 skip。
    # DDL 走 peewee migrator 抽象接口而不是 raw SQL，保持方言无关；
    # 只有回填的 UPDATE 用 execute_sql，占位符 %s 是 DB-API 标准，pg/mysql 都通用。
    if not database.table_exists("media_library"):
        raise SkipMigration("media_library table does not exist")

    # 1) backend 列：字段本身 NOT NULL + default='local'，peewee 生成的 DDL 会带 DEFAULT，
    #    已有行由方言引擎一次性回填。
    if not _column_exists(database, table_name="media_library", column_name="backend"):
        run_migration(
            migrator.add_column(
                "media_library",
                "backend",
                MediaLibrary._meta.fields["backend"],
            )
        )

    # 2) backend_config 列：临时用 nullable 版本加进来，回填后再 add_not_null。
    #    直接用 MediaLibrary._meta.fields["backend_config"]（null=False + default=dict 不可 SQL 化）
    #    会导致对已有行加 NOT NULL 列失败。
    if not _column_exists(database, table_name="media_library", column_name="backend_config"):
        run_migration(
            migrator.add_column(
                "media_library",
                "backend_config",
                JsonTextField(null=True, default=dict),
            )
        )

    # 3) 回填 backend_config：读旧 root_path，Python 序列化后写回。
    #    只在 root_path 列还在、且行的 backend_config 尚为空时回填，天然幂等。
    if _column_exists(database, table_name="media_library", column_name="root_path"):
        cursor = database.execute_sql(
            "SELECT id, root_path FROM media_library WHERE backend_config IS NULL"
        )
        rows = cursor.fetchall()
        for row_id, root_path in rows:
            payload = json.dumps({"root_path": root_path or ""}, ensure_ascii=False)
            database.execute_sql(
                "UPDATE media_library SET backend_config = %s WHERE id = %s",
                (payload, row_id),
            )

    # 4) 补 NOT NULL 约束（幂等：对已 NOT NULL 的列再 SET NOT NULL 无副作用）。
    run_migration(migrator.add_not_null("media_library", "backend_config"))

    # 5) 删掉 root_path 列（peewee 会自动带走 UNIQUE 索引）。
    if _column_exists(database, table_name="media_library", column_name="root_path"):
        run_migration(migrator.drop_column("media_library", "root_path"))
