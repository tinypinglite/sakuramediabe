from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260729_06_reset_media_thumbnail_states"


def migrate(database, migrator) -> None:
    """任务架构 Wave 2：缩略图任务迁 kernel 记账，清空重建 + 播种成功记忆（决策 #11）。

    执行侧有领域护栏（缩略图文件已存在即跳过并回写成功），清空本身安全；
    播种一步把"已有缩略图的有效媒体"直接写成 succeeded，省掉清空后全库
    空转一轮的数据库开销。已判死的坏文件会按新预算（2 次/轮）重试后重新收敛。
    """
    required_tables = {"resource_task_state", "media", "media_thumbnail"}
    missing_tables = sorted(
        table_name for table_name in required_tables if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key = %s",
        ("media_thumbnail_generation",),
    )
    database.execute_sql(
        "INSERT INTO resource_task_state"
        " (created_at, updated_at, task_key, resource_type, resource_id, state,"
        "  attempt_count, retry_round, last_succeeded_at)"
        " SELECT now(), now(), %s, 'media', m.id, 'succeeded', 0, 0, now()"
        " FROM media m"
        " WHERE m.valid = TRUE AND EXISTS ("
        "   SELECT 1 FROM media_thumbnail t WHERE t.media_id = m.id"
        " )",
        ("media_thumbnail_generation",),
    )
