from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260731_01_add_resource_task_attempt_finished_at_index"


def _ensure_index(database, *, table_name: str, columns: tuple[str, ...], index_name: str) -> None:
    # 按列组合判断是否已存在，兼容新库 create_tables 生成的同构索引（名字可能不同）。
    want = list(columns)
    for index in database.get_indexes(table_name):
        if list(index.columns) == want:
            return
    quoted_columns = ", ".join(f'"{column}"' for column in want)
    database.execute_sql(f'CREATE INDEX "{index_name}" ON "{table_name}" ({quoted_columns})')


def migrate(database, migrator) -> None:
    """为 resource_task_attempt 补 finished_at 单列索引。

    保留期清理走 `finished_at < cutoff` 定位过期尝试记录，缺索引时会全表扫，
    30w 影片规模下 attempt 表膨胀后清理会拖慢 vacuum。
    """
    if not database.table_exists("resource_task_attempt"):
        raise SkipMigration("resource_task_attempt table does not exist")
    _ensure_index(
        database,
        table_name="resource_task_attempt",
        columns=("finished_at",),
        index_name="resource_task_attempt_finished_at",
    )
