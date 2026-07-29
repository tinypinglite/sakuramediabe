from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260729_04_reset_movie_translation_states"


def migrate(database, migrator) -> None:
    """任务架构 Wave 2：两个翻译任务迁 kernel 记账，存量状态行清空重建（决策 #11）。

    翻译的"已完成"记忆可从领域数据重建（译文非空即不再是候选）。旧实现失败行
    无预算、每轮无限重试；清空后由 kernel 的 RetryPolicy（5 次/轮 + 指数退避）接管。
    """
    if not database.table_exists("resource_task_state"):
        raise SkipMigration("resource_task_state table does not exist")
    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key IN (%s, %s)",
        ("movie_desc_translation", "movie_title_translation"),
    )
