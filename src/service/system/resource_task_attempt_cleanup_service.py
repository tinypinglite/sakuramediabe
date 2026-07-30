from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.model import ResourceTaskAttempt
from src.model.base import get_database

# 30w 影片规模下 attempt 表按每日几万行速度膨胀，只用于人肉排查历史尝试，
# 保留 30 天足够定位近期失败模式；ResourceTaskState 本身仍保留最新错误信息不受影响。
_RETENTION_DAYS = 30
# 分批删避免首次跑一次锁太多行 / WAL 暴涨（30w 影片存量首跑可能删数百万行）。
_BATCH_SIZE = 10_000


class ResourceTaskAttemptCleanupService:
    """定时清理 resource_task_attempt 表的历史尝试记录。

    这张表只增不改，投影表 ResourceTaskState 通过 last_attempt 外键（SET NULL）
    指向最新一次尝试，历史行没有生产读路径、仅供人肉排查。超过保留窗口的终态
    行直接批量删除；PostgreSQL 会自动把指向被删行的 last_attempt_id 置空。
    """

    def cleanup(self) -> dict[str, int]:
        cutoff = utc_now_for_db() - timedelta(days=_RETENTION_DAYS)
        deleted_total = 0
        # 单次事务只删一批，让 WAL / vacuum 有喘息窗口；LIMIT 走 finished_at 索引定位。
        while True:
            stale_ids = ResourceTaskAttempt.select(ResourceTaskAttempt.id).where(
                ResourceTaskAttempt.finished_at < cutoff
            ).limit(_BATCH_SIZE)
            with get_database().atomic():
                deleted = ResourceTaskAttempt.delete().where(
                    ResourceTaskAttempt.id.in_(stale_ids)
                ).execute()
            if deleted == 0:
                break
            deleted_total += deleted
        stats = {"deleted_attempts": deleted_total}
        logger.info("Resource task attempt cleanup finished: {}", stats)
        return stats
