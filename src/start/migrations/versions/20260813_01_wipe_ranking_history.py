from __future__ import annotations

from src.start.migrations import SkipMigration

name = "20260813_01_wipe_ranking_history"


def migrate(database, migrator) -> None:
    """排行榜插件化：清空内置 javdb 来源写下的全部榜单数据与任务台账。

    旧排行快照冻结后不再有刷新来源，且每日推荐仍按 period 权重读榜单条目，
    保留会让过期信号持续污染推荐。榜单能力完全交给用户显式启用的排行榜插件，
    因此连同 ranking_sync 的运行记录、关联通知与事件一起清掉，不留尾巴。
    RankingItem 表结构、模型与读 API 保留，供插件从零重建。
    """
    required_tables = {
        "background_task_run",
        "ranking_item",
        "resource_task_attempt",
        "resource_task_state",
        "system_event",
        "system_notification",
    }
    missing_tables = sorted(
        table_name
        for table_name in required_tables
        if not database.table_exists(table_name)
    )
    if missing_tables:
        raise SkipMigration(f"required tables do not exist: {missing_tables}")

    # 先删指向待删 run 的通知：活动清理会把已删 run 的通知外键置空，
    # 因此同时按标题补齐那些已经失去关联的登录失败通知。
    database.execute_sql(
        "DELETE FROM system_notification"
        " WHERE related_task_run_id IN"
        " (SELECT id FROM background_task_run WHERE task_key = %s)"
        " OR title = %s",
        ("ranking_sync", "JavDB 账号登录失败"),
    )
    # 事件流只删指向本次 run 的 task_run 事件；payload 孤儿交给保留期清理。
    database.execute_sql(
        "DELETE FROM system_event"
        " WHERE resource_type = 'task_run'"
        " AND resource_id IN"
        " (SELECT id FROM background_task_run WHERE task_key = %s)",
        ("ranking_sync",),
    )
    # 防御性清残留资源状态：旧 ranking_sync 是整榜批任务，预期这两张表没有行。
    database.execute_sql(
        "DELETE FROM resource_task_attempt WHERE task_key = %s", ("ranking_sync",)
    )
    database.execute_sql(
        "DELETE FROM resource_task_state WHERE task_key = %s", ("ranking_sync",)
    )
    database.execute_sql(
        "DELETE FROM background_task_run WHERE task_key = %s", ("ranking_sync",)
    )
    database.execute_sql("DELETE FROM ranking_item")
