from __future__ import annotations

from playhouse.migrate import migrate as run_migration

from src.model import BackgroundTaskRun, ResourceTaskAttempt, ResourceTaskState
from src.start.migrations import SkipMigration

name = "20260729_01_add_task_queue_and_attempts"


def _column_exists(database, *, table_name: str, column_name: str) -> bool:
    return any(column.name == column_name for column in database.get_columns(table_name))


def _has_foreign_key(database, *, table_name: str, column_name: str) -> bool:
    cursor = database.execute_sql(
        """
        SELECT 1
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.constraint_schema = kcu.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = current_schema()
          AND tc.table_name = %s
          AND kcu.column_name = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return cursor.fetchone() is not None


def _ensure_index(database, *, table_name: str, columns: tuple[str, ...], index_name: str) -> None:
    # 按列组合判断是否已存在，兼容新库 create_tables 生成的同构索引（名字可能不同）。
    want = list(columns)
    for index in database.get_indexes(table_name):
        if list(index.columns) == want:
            return
    quoted_columns = ", ".join(f'"{column}"' for column in want)
    database.execute_sql(f'CREATE INDEX "{index_name}" ON "{table_name}" ({quoted_columns})')


def migrate(database, migrator) -> None:
    """任务架构 Wave 0（docs/development/task-architecture.md）：纯加列，向后兼容。

    - background_task_run 队列化扩列：params / scheduled_at / lease_expires_at
    - 新增 resource_task_attempt 尝试历史表
    - resource_task_state 扩列：next_retry_at / error_code / retry_round / last_attempt_id
    - last_task_run_id 由裸整数外键化（先清悬空引用再补 ON DELETE SET NULL 约束）
    """
    required_tables = {"background_task_run", "resource_task_state"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    for column_name in ("params", "scheduled_at", "lease_expires_at"):
        if not _column_exists(database, table_name="background_task_run", column_name=column_name):
            run_migration(
                migrator.add_column(
                    "background_task_run",
                    column_name,
                    BackgroundTaskRun._meta.fields[column_name],
                )
            )

    # 尝试历史表：新库由 initdb 建出，这里兜底存量库。
    database.create_tables([ResourceTaskAttempt], safe=True)

    # last_attempt_id 是 FK 列，playhouse 会随 add_column 一并补外键约束与索引。
    for column_name, field_name in (
        ("next_retry_at", "next_retry_at"),
        ("error_code", "error_code"),
        ("retry_round", "retry_round"),
        ("last_attempt_id", "last_attempt"),
    ):
        if not _column_exists(database, table_name="resource_task_state", column_name=column_name):
            run_migration(
                migrator.add_column(
                    "resource_task_state",
                    column_name,
                    ResourceTaskState._meta.fields[field_name],
                )
            )

    # 外键化前先清悬空引用：活动清理删 task_run 后，裸整数列会残留已删除行的 id。
    database.execute_sql(
        "UPDATE resource_task_state SET last_task_run_id = NULL "
        "WHERE last_task_run_id IS NOT NULL AND NOT EXISTS ("
        "  SELECT 1 FROM background_task_run r"
        "  WHERE r.id = resource_task_state.last_task_run_id"
        ")"
    )
    if not _has_foreign_key(
        database, table_name="resource_task_state", column_name="last_task_run_id"
    ):
        database.execute_sql(
            "ALTER TABLE resource_task_state "
            "ADD CONSTRAINT resource_task_state_last_task_run_id_fk "
            "FOREIGN KEY (last_task_run_id) REFERENCES background_task_run (id) "
            "ON DELETE SET NULL"
        )

    _ensure_index(
        database,
        table_name="background_task_run",
        columns=("state", "scheduled_at"),
        index_name="background_task_run_state_scheduled_at",
    )
    _ensure_index(
        database,
        table_name="resource_task_state",
        columns=("task_key", "state", "next_retry_at"),
        index_name="resource_task_state_task_key_state_next_retry_at",
    )
