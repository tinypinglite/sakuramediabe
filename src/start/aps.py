from __future__ import annotations

from typing import Any, Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src.common.database import ensure_database_ready
from src.common.runtime_time import get_runtime_timezone, get_runtime_timezone_name
from src.config.config import Scheduler, settings
from src.scheduler.registry import JOB_REGISTRY, JobDefinition
from src.service.system import ActivityService
from src.start.recovery import recover_interrupted_tasks

INTERRUPTED_TASK_RUN_ERROR_MESSAGE = "任务执行中断，等待重试"


def _resolve_scheduler_cron_expr(cron_setting: str) -> str:
    cron_expr = getattr(settings.scheduler, cron_setting, None)
    if cron_expr is not None:
        return cron_expr
    # 兼容运行时 settings 对象尚未带上新增 cron 字段的场景，回退到默认配置。
    return getattr(Scheduler(), cron_setting)


def run_job(
    job_def: JobDefinition,
    *,
    trigger_type: str = "scheduled",
    extra_callbacks: list[Callable[[dict[str, Any]], None]] | None = None,
) -> Any:
    """通用任务执行入口，供 APS 定时触发和 CLI 手动触发共用。"""
    ensure_database_ready()

    # 统一先回收当前 task_key 遗留的 task_run，确保 stale mutex 不会卡死后续调度。
    recovered_task_runs = ActivityService.recover_interrupted_task_runs(
        task_key=job_def.task_key,
        error_message=INTERRUPTED_TASK_RUN_ERROR_MESSAGE,
        allow_null_owner=True,
    )
    recovery_stats: dict[str, int] = {
        "recovered_task_runs": len(recovered_task_runs),
    }
    if recovered_task_runs and job_def.business_recovery:
        recovery_stats.update(job_def.business_recovery())

    func = job_def.service_factory
    if recovered_task_runs:
        original_func = func

        def func_with_recovery(reporter):
            result = original_func(reporter)
            if isinstance(result, dict):
                result.update(recovery_stats)
                return result
            return result

        func = func_with_recovery

    conflict_policy = "raise" if trigger_type == "manual" else "skip"
    result = ActivityService.run_task(
        task_key=job_def.task_key,
        trigger_type=trigger_type,
        func=func,
        log_task_name=job_def.log_name,
        extra_callbacks=extra_callbacks,
        mutex_key=f"aps:{job_def.task_key}" if trigger_type in {"manual", "scheduled"} else None,
        conflict_policy=conflict_policy,
    )
    if (
        trigger_type == "scheduled"
        and isinstance(result, dict)
        and result.get("task_skipped") is True
        and result.get("reason") == "mutex_conflict"
    ):
        logger.info(
            "定时任务因同任务仍在运行而跳过 task_key={} blocking_task_run_id={} blocking_trigger_type={}",
            job_def.task_key,
            result.get("blocking_task_run_id"),
            result.get("blocking_trigger_type"),
        )
    return result


def build_scheduler() -> BlockingScheduler:
    timezone = get_runtime_timezone()
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(4)},
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone=timezone,
    )
    for job_def in JOB_REGISTRY:
        cron_expr = _resolve_scheduler_cron_expr(job_def.cron_setting)
        scheduler.add_job(
            run_job,
            args=[job_def],
            trigger=CronTrigger.from_crontab(cron_expr, timezone=timezone),
            id=job_def.task_key,
            replace_existing=True,
        )
    return scheduler


def aps():
    if not settings.scheduler.enabled:
        logger.info("Scheduler is disabled by configuration")
        return
    database = ensure_database_ready()
    logger.info("Scheduler runtime database ready {}", type(database).__name__)
    # APS 进程启动时统一回收由该进程负责的任务类型，并联动清理业务侧 running 状态。
    recover_interrupted_tasks(
        trigger_types=("scheduled", "manual", "internal"),
        error_message="APS进程重启，任务已中断",
    )
    scheduler = build_scheduler()
    cron_info = " ".join(
        f"{j.cron_setting}={_resolve_scheduler_cron_expr(j.cron_setting)}" for j in JOB_REGISTRY
    )
    logger.info("Starting scheduler runtime_timezone={} {}", get_runtime_timezone_name(), cron_info)
    scheduler.start()
