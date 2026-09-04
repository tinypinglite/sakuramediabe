from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from src.service.transfers.shared.import_task_service import ImportTaskService

# 注册表: task_key -> 业务层回收 callable。
# 启动恢复在任务层 (BackgroundTaskRun) 回收之后，按 task_key 查表联动清理业务状态。
BUSINESS_RECOVERY_HANDLERS: dict[str, Callable[[], object]] = {
    "library_import": ImportTaskService.recover_interrupted_downloads,
}

def recover_business_states(task_keys: set[str]) -> None:
    """优先调用 JobDefinition 声明的恢复钩子，队列专属任务再查宿主注册表。"""
    from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY
    from src.scheduler.registry import JOB_REGISTRY_BY_KEY

    for task_key in sorted(task_keys):
        job_def = JOB_REGISTRY_BY_KEY.get(task_key) or QUEUE_TASK_REGISTRY.get(task_key)
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
