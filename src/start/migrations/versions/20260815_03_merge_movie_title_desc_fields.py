from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.start.migrations import SkipMigration

name = "20260815_03_merge_movie_title_desc_fields"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def migrate(database, migrator) -> None:
    """DMM 简介抓取与翻译链路下线后，desc / desc_zh / title_zh 已无任何写入方，收拢存量后删列。

    数据迁移规则（按用户既定优先级，desc_zh 中文描述优先于 desc）：
    - title_zh 非空 → title 覆盖为中文标题；
    - desc_zh 非空 → summary 覆盖为中文描述；desc_zh 为空但 desc 非空 → summary = desc；
    - 两者都为空 → summary 保持原样不动。
    """
    if not database.table_exists("movie"):
        raise SkipMigration("movie table does not exist")

    has_title_zh = _column_exists(database, table_name="movie", column_name="title_zh")
    has_desc = _column_exists(database, table_name="movie", column_name="desc")
    has_desc_zh = _column_exists(database, table_name="movie", column_name="desc_zh")

    # 数据迁移必须在删列之前完成；"有值"统一按 BTRIM(COALESCE(..., '')) <> '' 判定：
    # 既兼容极端情况下遗留的 NULL 行（避免 NULL 值随删列被静默丢弃），
    # 也排除纯空白串（防止空白文本覆盖真实 title / summary）。
    if has_title_zh:
        database.execute_sql(
            "UPDATE movie SET title = title_zh WHERE BTRIM(COALESCE(title_zh, '')) <> ''"
        )
    if has_desc_zh:
        database.execute_sql(
            "UPDATE movie SET summary = desc_zh WHERE BTRIM(COALESCE(desc_zh, '')) <> ''"
        )
    if has_desc:
        if has_desc_zh:
            database.execute_sql(
                """
                UPDATE movie SET summary = "desc"
                WHERE BTRIM(COALESCE(desc_zh, '')) = '' AND BTRIM(COALESCE("desc", '')) <> ''
                """
            )
        else:
            database.execute_sql(
                "UPDATE movie SET summary = \"desc\" WHERE BTRIM(COALESCE(\"desc\", '')) <> ''"
            )

    operations = []
    if has_title_zh:
        operations.append(migrator.drop_column("movie", "title_zh"))
    if has_desc:
        operations.append(migrator.drop_column("movie", "desc"))
    if has_desc_zh:
        operations.append(migrator.drop_column("movie", "desc_zh"))
    if not operations:
        return
    run_migration(*operations)
