from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260729_02_wipe_task_run_history"


def migrate(database, migrator) -> None:
    """任务架构 Wave 1 切换：清空 task_run / system_event 历史（决策 #11）。

    旧执行内核的运行记录是纯观测数据（活动清理服务平时也在按保留期删），新内核
    的活动流从零起点开始，不做旧数据兼容展示。引用方（ImportJob.task_run、
    通知关联、resource_task_state.last_task_run_id）全部是 ON DELETE SET NULL
    外键，随删除自动置空。迁移跑在容器入口、进程启动之前，不存在在跑任务。
    """
    required_tables = {"background_task_run", "system_event"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    database.execute_sql("DELETE FROM system_event")
    database.execute_sql("DELETE FROM background_task_run")
