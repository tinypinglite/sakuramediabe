from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import Media
from src.start.migrations import SkipMigration

name = "20260714_01_add_media_backend_locator"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _column_nullable(database, *, table_name: str, column_name: str) -> bool:
    for column in database.get_columns(table_name):
        if column.name == column_name:
            return column.null
    return False


def _index_exists(database, *, table_name: str, columns: tuple[str, ...]) -> bool:
    return any(tuple(index.columns) == columns for index in database.get_indexes(table_name))


def migrate(database, migrator) -> None:
    # media 表加 backend_locator（cloud115 / SMB 等云盘 backend 用），并把 path 放松为可空
    # （本地库仍用 path，cloud115 库 path 留空）。新装用户 initdb 已直接建出新 schema，
    # 每一步都幂等，命中已完成态时全部 skip。DDL 全走 peewee migrator 抽象接口，方言无关。
    if not database.table_exists("media"):
        raise SkipMigration("media table does not exist")

    # 1) backend_locator 列：JsonTextField 底层是 TEXT，nullable + default=None，
    #    已有本地行 backend_locator 保持 NULL，无需回填。
    if not _column_exists(database, table_name="media", column_name="backend_locator"):
        run_migration(
            migrator.add_column(
                "media",
                "backend_locator",
                Media._meta.fields["backend_locator"],
            )
        )

    # 2) 放松 path 为可空：PostgreSQL 上 unique 约束对 NULL 不生效，
    #    本地行的 path 依然唯一，cloud115 行 path=NULL 不参与唯一性。
    if not _column_nullable(database, table_name="media", column_name="path"):
        run_migration(migrator.drop_not_null("media", "path"))

    # 3) (library_id, backend_locator) 复合唯一索引：同库同 locator 只允许登记一条。
    #    locator dict 由 service 层统一构造（键序固定），TEXT 相等判定稳定；
    #    本地行 backend_locator=NULL 不参与唯一约束。
    if not _index_exists(database, table_name="media", columns=("library_id", "backend_locator")):
        run_migration(
            migrator.add_index("media", ("library_id", "backend_locator"), True)
        )
