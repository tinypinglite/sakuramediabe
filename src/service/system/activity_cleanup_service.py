from __future__ import annotations

from datetime import timedelta

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import BackgroundTaskRun, SystemNotification
from src.model.base import get_database


class ActivityCleanupService:
    """活动中心任务运行与通知的保留期清理。

    这两张表会随后台任务与业务通知持续增长，需要定期回收：
    - background_task_run 每个 task_key 只保留最近若干条运行记录，供任务中心翻页，更旧的删除。
    - system_notification 把已读且超过保留天数的通知删除，未读通知一律保留。

    只删数据、不改表结构；阈值定位用 limit/offset 而非窗口函数，避免清理逻辑过度依赖复杂 SQL。
    """

    def cleanup(self) -> dict[str, int]:
        scheduler_settings = settings.scheduler
        deleted_task_runs = self._cleanup_task_runs(
            scheduler_settings.activity_task_run_retention_per_key
        )
        deleted_notifications = self._cleanup_notifications(
            scheduler_settings.activity_notification_read_retention_days
        )
        stats = {
            "deleted_task_runs": deleted_task_runs,
            "deleted_notifications": deleted_notifications,
        }
        logger.info("Activity record cleanup finished: {}", stats)
        return stats

    def _cleanup_task_runs(self, retention_per_key: int) -> int:
        deleted_total = 0
        terminal_states = ("completed", "failed")
        task_keys = [
            row.task_key
            for row in BackgroundTaskRun.select(BackgroundTaskRun.task_key).distinct()
        ]
        for task_key in task_keys:
            # 保留额只在终态历史内计算；pending/running 无论 id 多旧都不能被清理。
            threshold_id = (
                BackgroundTaskRun.select(BackgroundTaskRun.id)
                .where(
                    BackgroundTaskRun.task_key == task_key,
                    BackgroundTaskRun.state.in_(terminal_states),
                )
                .order_by(BackgroundTaskRun.id.desc())
                .limit(1)
                .offset(retention_per_key)
                .scalar()
            )
            if threshold_id is None:
                # 该 task_key 的运行记录尚不足 retention_per_key 条，无需清理。
                continue
            stale_condition = (
                (BackgroundTaskRun.task_key == task_key)
                & (BackgroundTaskRun.state.in_(terminal_states))
                & (BackgroundTaskRun.id <= threshold_id)
            )
            with get_database().atomic():
                stale_ids = BackgroundTaskRun.select(BackgroundTaskRun.id).where(
                    stale_condition
                )
                # 先把指向待删运行记录的通知外键置空，避免悬挂引用，不依赖数据库级联行为。
                SystemNotification.update(related_task_run=None).where(
                    SystemNotification.related_task_run.in_(stale_ids)
                ).execute()
                deleted_total += (
                    BackgroundTaskRun.delete().where(stale_condition).execute()
                )
        return deleted_total

    def _cleanup_notifications(self, retention_days: int) -> int:
        # 只清理已读且读取时间超过保留窗口的通知；未读通知（read_at 为空）一律保留。
        cutoff = utc_now_for_db() - timedelta(days=retention_days)
        with get_database().atomic():
            return (
                SystemNotification.delete()
                .where(
                    (SystemNotification.is_read == True)
                    & (SystemNotification.read_at < cutoff)
                )
                .execute()
            )
