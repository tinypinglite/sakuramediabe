from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from src.service.system import ActivityService
from src.service.transfers.rapid_upload.facade import MediaRapidUploadService
from src.service.transfers.shared.import_task_service import ImportTaskService

# 注册表: task_key -> 业务层回收 callable。
# 启动恢复在任务层 (BackgroundTaskRun) 回收之后，按 task_key 查表联动清理业务状态。
BUSINESS_RECOVERY_HANDLERS: dict[str, Callable[[], object]] = {
    "library_import": ImportTaskService.recover_interrupted_downloads,
    "media_rapid_upload": lambda: MediaRapidUploadService.recover_interrupted_batches(),
}

# 秒传批次需要在业务恢复完成、统计已收敛后才能发送一条汇总通知。
BUSINESS_MANAGED_NOTIFICATION_TASK_KEYS = {"media_rapid_upload"}


def recover_business_states(task_keys: set[str]) -> None:
    """优先调用 JobDefinition 声明的恢复钩子，队列专属任务再查宿主注册表。"""
    from src.scheduler.registry import JOB_REGISTRY_BY_KEY

    for task_key in sorted(task_keys):
        job_def = JOB_REGISTRY_BY_KEY.get(task_key)
        handler = (
            job_def.business_recovery
            if job_def is not None and job_def.business_recovery is not None
            else BUSINESS_RECOVERY_HANDLERS.get(task_key)
        )
        if handler is None:
            continue
        try:
            logger.info(
                "Recovering business state task_key={} stats={}", task_key, handler()
            )
        except Exception:
            logger.exception("Business recovery failed task_key={}", task_key)


def recover_interrupted_tasks(
    *,
    trigger_types: tuple[str, ...],
    error_message: str,
) -> set[str]:
    """启动时回收中断的任务并联动清理业务状态。

    Phase 1: 按 trigger_type 逐一扫描 pending/running 的 BackgroundTaskRun，标记为 failed。
    Phase 2: 对回收到的 task_key，查注册表调用对应的业务层回收逻辑。
    """
    recovered_task_keys: set[str] = set()
    for trigger_type in trigger_types:
        for task_run in ActivityService.recover_interrupted_task_runs(
            trigger_type=trigger_type,
            error_message=error_message,
            allow_null_owner=True,
            force=True,
            suppress_notification_task_keys=BUSINESS_MANAGED_NOTIFICATION_TASK_KEYS,
        ):
            recovered_task_keys.add(task_run.task_key)

    recover_business_states(recovered_task_keys)

    return recovered_task_keys
