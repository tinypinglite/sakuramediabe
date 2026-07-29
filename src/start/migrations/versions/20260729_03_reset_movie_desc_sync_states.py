from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260729_03_reset_movie_desc_sync_states"


def migrate(database, migrator) -> None:
    """任务架构 Wave 2：movie_desc_sync 迁 kernel 记账，存量状态行清空重建（决策 #11）。

    该任务的"已完成"记忆可从领域数据重建（desc 非空即不再是候选），状态行只是
    过滤器；旧 `failed + extra.terminal` 词汇不做映射迁移。DMM 确认无此番号的影片
    会在下一轮各多请求一次后重新落 failed_terminal，一次性成本、自愈收敛。
    """
    if not database.table_exists("resource_task_state"):
        raise SkipMigration("resource_task_state table does not exist")
    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key = %s", ("movie_desc_sync",)
    )
