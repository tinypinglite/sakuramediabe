from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260816_01_add_movie_field_owners"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    """v2-lite 字段主权：movie 增加 field_owners / mutation_revision 两列。

    使用 PostgreSQL 服务端 DEFAULT（常量默认值走 metadata-only fast path），
    避免 30w 行量级下 ALTER TABLE 重写整表；新库由 initdb 的 create_tables
    按模型渲染出同构列，此处仅服务存量库。
    """
    if not database.table_exists("movie"):
        raise SkipMigration("movie table does not exist")
    if _column_exists(database, table_name="movie", column_name="field_owners") and (
        _column_exists(database, table_name="movie", column_name="mutation_revision")
    ):
        return
    database.execute_sql(
        """
        ALTER TABLE movie
          ADD COLUMN IF NOT EXISTS field_owners JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS mutation_revision BIGINT NOT NULL DEFAULT 0
        """
    )
