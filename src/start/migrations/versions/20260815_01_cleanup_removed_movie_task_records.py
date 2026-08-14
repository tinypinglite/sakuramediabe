from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260815_01_cleanup_removed_movie_task_records"

# 已整体下线的三个任务：DMM 简介抓取（movie_desc_sync）与影片字段翻译
# （movie_desc_translation / movie_title_translation）。注册表与业务代码均已删除，
# 库里遗留的状态行/尝试历史/运行记录/关联通知属于纯孤儿数据，一并清掉。
REMOVED_TASK_KEYS = (
    "movie_desc_sync",
    "movie_desc_translation",
    "movie_title_translation",
)


def migrate(database, migrator) -> None:
    """清理已下线任务的 resource_task_state / resource_task_attempt / background_task_run 与关联通知。

    顺序无关外键安全（state.last_attempt / state.last_task_run / attempt.task_run /
    notification.related_task_run 全部是 ON DELETE SET NULL），按"先通知、后状态、
    再尝试、最后运行记录"的依赖方向删除；通知直接删除而非留 SET NULL 空壳，
    与 ActivityCleanupService 保留已读通知的语义不同——任务本体已不存在，通知无展示价值。
    """
    required_tables = {
        "resource_task_state",
        "resource_task_attempt",
        "background_task_run",
        "system_notification",
        "system_event",
    }
    missing_tables = sorted(
        table_name
        for table_name in required_tables
        if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    placeholders = ", ".join(["%s"] * len(REMOVED_TASK_KEYS))

    # 1. 关联通知：指向已下线任务运行记录的通知一并删除（否则只剩 related_task_run 置空后的空壳）。
    database.execute_sql(
        f"DELETE FROM system_notification WHERE related_task_run_id IN ("
        f"SELECT id FROM background_task_run WHERE task_key IN ({placeholders})"
        f")",
        REMOVED_TASK_KEYS,
    )
    # 2. 事件流只删指向本次 run 的 task_run 事件（先于 run 删除执行，子查询才能命中）；
    #    其余 payload 孤儿交给保留期清理。
    database.execute_sql(
        f"DELETE FROM system_event WHERE resource_type = 'task_run' AND resource_id IN ("
        f"SELECT id FROM background_task_run WHERE task_key IN ({placeholders})"
        f")",
        REMOVED_TASK_KEYS,
    )
    # 3. 资源状态行（含 last_attempt_id / last_task_run_id 外键引用）。
    database.execute_sql(
        f"DELETE FROM resource_task_state WHERE task_key IN ({placeholders})",
        REMOVED_TASK_KEYS,
    )
    # 4. 单次尝试历史。
    database.execute_sql(
        f"DELETE FROM resource_task_attempt WHERE task_key IN ({placeholders})",
        REMOVED_TASK_KEYS,
    )
    # 5. 任务运行记录（活动清理按 task_key 保留 200 条，不会全清，这里一次性清干净）。
    database.execute_sql(
        f"DELETE FROM background_task_run WHERE task_key IN ({placeholders})",
        REMOVED_TASK_KEYS,
    )
