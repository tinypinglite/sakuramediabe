from __future__ import annotations

import threading
import time
from typing import Any, Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from peewee import IntegrityError

from src.common.database import ensure_database_ready
from src.common.runtime_time import get_runtime_timezone, get_runtime_timezone_name
from src.config.config import Scheduler, settings
from src.model import BackgroundTaskRun
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.service.system import ActivityService
from src.service.system.activity_service import TaskRunConflictError
from src.start.recovery import recover_interrupted_tasks

INTERRUPTED_TASK_RUN_ERROR_MESSAGE = "任务执行中断，等待重试"


def get_job_cron_setting(job_def: JobDefinition) -> str:
    """返回对外展示的 cron 配置路径。"""
    if job_def.plugin_id is not None:
        return f"plugins.job_crons.{job_def.plugin_id}.{job_def.task_key}"
    if job_def.cron_setting is None:
        raise RuntimeError(f"内建任务缺少 cron_setting task_key={job_def.task_key}")
    return job_def.cron_setting


def resolve_job_cron_expr(job_def: JobDefinition) -> str:
    """解析内建任务静态配置或插件任务显式覆盖后的 cron。"""
    if job_def.plugin_id is not None:
        cron_expr = (
            settings.plugins.job_crons
            .get(job_def.plugin_id, {})
            .get(job_def.task_key)
        )
        if cron_expr is not None:
            return cron_expr
        if job_def.default_cron is not None:
            return job_def.default_cron
        raise RuntimeError(f"插件任务缺少 cron task_key={job_def.task_key}")

    if job_def.cron_setting is None:
        if job_def.default_cron is not None:
            return job_def.default_cron
        raise RuntimeError(f"内建任务缺少 cron task_key={job_def.task_key}")
    cron_expr = getattr(settings.scheduler, job_def.cron_setting, None)
    if cron_expr is not None:
        return cron_expr
    # 兼容运行时 settings 对象尚未带上新增 cron 字段的场景，回退到默认配置。
    return getattr(Scheduler(), job_def.cron_setting)


def _prepare_recovery(job_def: JobDefinition) -> tuple[Callable[..., Any], dict[str, int], int]:
    """统一执行 stale task_run 回收，并把回收统计折叠进任务结果。"""
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

    return func, recovery_stats, len(recovered_task_runs)


def run_job(
    job_def: JobDefinition,
    *,
    trigger_type: str = "scheduled",
    extra_callbacks: list[Callable[[dict[str, Any]], None]] | None = None,
) -> Any:
    """通用任务执行入口，供 APS 定时触发和 CLI 手动触发共用。"""
    ensure_database_ready()

    # 统一先回收当前 task_key 遗留的 task_run，确保 stale mutex 不会卡死后续调度。
    func, _recovery_stats, _recovered_count = _prepare_recovery(job_def)

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


def submit_manual_job(job_def: JobDefinition) -> BackgroundTaskRun:
    """在当前进程内同步预占 task_run 并启动后台线程执行，返回新建的 task_run。

    冲突时抛 ``TaskRunConflictError``；调用方负责把它映射为 HTTP 响应。
    """
    ensure_database_ready()

    func, _recovery_stats, _recovered_count = _prepare_recovery(job_def)

    mutex_key = f"aps:{job_def.task_key}"
    try:
        task_run = ActivityService.create_task_run(
            task_key=job_def.task_key,
            trigger_type="manual",
            mutex_key=mutex_key,
        )
    except IntegrityError as exc:
        blocking_task_run = ActivityService.find_task_run_by_mutex_key(mutex_key)
        if blocking_task_run is None:
            raise
        raise TaskRunConflictError(blocking_task_run) from exc

    task_run_id = task_run.id

    def _runner() -> None:
        try:
            ensure_database_ready()
            ActivityService.run_task(
                task_key=job_def.task_key,
                trigger_type="manual",
                func=func,
                task_run_id=task_run_id,
                log_task_name=job_def.log_name,
                mutex_key=mutex_key,
                conflict_policy="raise",
            )
        except Exception:
            # run_task 内部已经把异常写入 task_run；这里仅记录线程层日志，
            # 避免未捕获异常打断 daemon 线程的清理路径。
            logger.exception(
                "Manual job thread crashed task_key={} task_run_id={}",
                job_def.task_key,
                task_run_id,
            )

    thread = threading.Thread(
        target=_runner,
        name=f"manual-job-{job_def.task_key}-{task_run_id}",
        daemon=True,
    )
    thread.start()
    return task_run


def _bootstrap_first_playback_ranking(scheduler: BlockingScheduler) -> None:
    """首次部署引导：若 Movie 表为空，安排一次 javdb 热播榜 daily 抓取。

    - 目的：新库启动后立刻有内容，不必等凌晨 01:45 的定时 ranking_sync
    - 目标最小：只抓 javdb 免登录的 playback_all/daily，冷启动足够轻
    - 复用 ranking_sync 的 task_key 与 mutex，与定时触发天然互斥
    - 任何异常都吞掉：早期部署可能还没跑迁移，不能让引导逻辑打崩 APS 启动
    """
    try:
        from src.model.catalog.movies import Movie
        from src.service.discovery import RankingSyncService

        if Movie.select().count() > 0:
            return

        logger.info("首次部署检测：影片库为空，安排一次 javdb 热播榜（daily）抓取")

        ranking_sync_def = JOB_REGISTRY_BY_KEY["ranking_sync"]

        def _runner() -> None:
            ensure_database_ready()

            def _fetch(_reporter):
                return RankingSyncService().sync_board_period(
                    "javdb", "playback_all", "daily"
                )

            try:
                ActivityService.run_task(
                    task_key=ranking_sync_def.task_key,
                    trigger_type="startup",
                    func=_fetch,
                    log_task_name=ranking_sync_def.log_name,
                    mutex_key=f"aps:{ranking_sync_def.task_key}",
                    conflict_policy="skip",
                )
            except Exception:
                logger.exception("Bootstrap javdb playback ranking failed")

        # misfire_grace_time=None：避免默认 1s 宽限期把这次"立刻执行"的 date job
        # 在启动瞬时忙碌时静默判成 missed 而丢弃。
        scheduler.add_job(
            _runner,
            trigger="date",
            id="bootstrap_ranking_playback",
            replace_existing=True,
            misfire_grace_time=None,
        )
    except Exception:
        logger.exception("Skip bootstrap ranking due to unexpected error")


def _bootstrap_gfriends_filetree_refresh(scheduler: BlockingScheduler) -> None:
    """首次部署/缓存缺失时的引导：立刻拉一次 GFriends Filetree 到 disk cache。

    - 目的：避免首个 JavDB 详情请求触发同步网络阻塞
    - 触发条件：disk cache 不存在或已超过 TTL（等 cron 又要一周太久）
    - 与 cron 定时任务共享 task_key/mutex，不会重复触发
    - 任何异常都吞掉：GFriends 只是头像美化，不能让引导逻辑打崩 APS 启动
    """
    try:
        from pathlib import Path

        from src.metadata.factory import refresh_gfriends_filetree

        gfriends_refresh_def = JOB_REGISTRY_BY_KEY.get("gfriends_filetree_refresh")
        if gfriends_refresh_def is None:
            return

        cache_path = Path(settings.metadata.gfriends_filetree_cache_path).expanduser()
        if cache_path.exists():
            age_seconds = time.time() - cache_path.stat().st_mtime
            ttl_seconds = max(settings.metadata.gfriends_filetree_cache_ttl_hours, 1) * 3600
            if age_seconds <= ttl_seconds:
                # cache 仍新鲜，业务侧秒读，等 cron 定期刷新即可
                return

        logger.info("首次部署检测：GFriends Filetree 缓存缺失或过期，安排一次预热拉取")

        def _runner() -> None:
            ensure_database_ready()

            def _fetch(_reporter):
                return refresh_gfriends_filetree(force=False)

            try:
                ActivityService.run_task(
                    task_key=gfriends_refresh_def.task_key,
                    trigger_type="startup",
                    func=_fetch,
                    log_task_name=gfriends_refresh_def.log_name,
                    mutex_key=f"aps:{gfriends_refresh_def.task_key}",
                    conflict_policy="skip",
                )
            except Exception:
                logger.exception("Bootstrap gfriends filetree refresh failed")

        # misfire_grace_time=None：避免默认 1s 宽限期把这次"立刻执行"的 date job
        # 在启动瞬时忙碌时静默判成 missed 而丢弃。
        scheduler.add_job(
            _runner,
            trigger="date",
            id="bootstrap_gfriends_filetree_refresh",
            replace_existing=True,
            misfire_grace_time=None,
        )
    except Exception:
        logger.exception("Skip bootstrap gfriends filetree refresh due to unexpected error")


def build_scheduler() -> BlockingScheduler:
    timezone = get_runtime_timezone()
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(4)},
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone=timezone,
    )
    for job_def in JOB_REGISTRY:
        cron_expr = resolve_job_cron_expr(job_def)
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
    # startup 类型对应本进程内 `_bootstrap_first_playback_ranking` 排下的引导任务。
    recover_interrupted_tasks(
        trigger_types=("scheduled", "manual", "internal", "startup"),
        error_message="APS进程重启，任务已中断",
    )
    scheduler = build_scheduler()
    _bootstrap_first_playback_ranking(scheduler)
    _bootstrap_gfriends_filetree_refresh(scheduler)
    cron_info = " ".join(
        f"{get_job_cron_setting(j)}={resolve_job_cron_expr(j)}"
        for j in JOB_REGISTRY
    )
    logger.info("Starting scheduler runtime_timezone={} {}", get_runtime_timezone_name(), cron_info)
    scheduler.start()
