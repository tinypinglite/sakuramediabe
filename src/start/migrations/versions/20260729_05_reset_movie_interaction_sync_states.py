from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260729_05_reset_movie_interaction_sync_states"


def migrate(database, migrator) -> None:
    """任务架构 Wave 2：movie_interaction_sync 迁 kernel 记账（决策 #11 的例外任务）。

    该任务的时间分层依赖 `last_succeeded_at`，且领域数据里没有任何副本——清空
    等于重放全库 seed（每部影片一次 JavDB 请求），因此**保留成功记忆、只清历史**：
    - 有成功记录的行：state 归位 succeeded，清空错误/计数/重试字段；
    - 从未成功过的行（含 running/failed 残留）：直接删除，等下轮 seed 重建。
    """
    if not database.table_exists("resource_task_state"):
        raise SkipMigration("resource_task_state table does not exist")
    database.execute_sql(
        "DELETE FROM resource_task_state"
        " WHERE task_key = %s AND last_succeeded_at IS NULL",
        ("movie_interaction_sync",),
    )
    database.execute_sql(
        "UPDATE resource_task_state SET"
        " state = 'succeeded', attempt_count = 0, retry_round = 0,"
        " last_error = NULL, last_error_at = NULL, error_code = NULL,"
        " next_retry_at = NULL, extra = NULL"
        " WHERE task_key = %s",
        ("movie_interaction_sync",),
    )
