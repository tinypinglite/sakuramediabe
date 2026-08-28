from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src.common.database import ensure_database_ready
from src.common.runtime_time import (
    get_runtime_timezone,
    get_runtime_timezone_name,
    runtime_now,
    utc_now_for_db,
)
from src.config.config import Scheduler, settings
from src.model import BackgroundTaskRun
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.scheduler.worker import TaskWorker
from src.service.system.activity import TaskRunConflictError
from src.service.system.task_queue_service import (
    BOOTSTRAP_QUEUE_TASK_KEYS,
    DEFAULT_LEASE_SECONDS,
    FAILURE_CODE_QUEUE_LEASE_EXPIRED,
    INTERNAL_FAILURE_CODE_KEY,
    TaskQueueConflictError,
    TaskQueueService,
)

BOOTSTRAP_RETRY_GRACE_SECONDS = 1
BOOTSTRAP_ERROR_RETRY_SECONDS = 5


def get_job_cron_setting(job_def: JobDefinition) -> str | None:
    """返回对外展示的 cron 配置路径。"""
    if job_def.manual_only:
        return None
    if job_def.plugin_id is not None:
        return f"plugins.job_crons.{job_def.plugin_id}.{job_def.task_key}"
    if job_def.cron_setting is None:
        raise RuntimeError(f"内建任务缺少 cron_setting task_key={job_def.task_key}")
    return job_def.cron_setting


def resolve_job_cron_expr(job_def: JobDefinition) -> str | None:
    """解析内建任务静态配置或插件任务显式覆盖后的 cron。"""
    if job_def.manual_only:
        return None
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


def run_job(
    job_def: JobDefinition,
    *,
    trigger_type: str = "scheduled",
    params: dict[str, Any] | None = None,
) -> BackgroundTaskRun | None:
    """统一任务入口：只入队，实际执行始终由 worker 完成。"""
    ensure_database_ready()
    if trigger_type == "scheduled":
        return enqueue_scheduled_job(job_def)
    return submit_manual_job(job_def, params=params)


def enqueue_scheduled_job(job_def: JobDefinition) -> BackgroundTaskRun | None:
    """APS cron 触发只入队不执行，由 worker 领取；执行位置与调度解耦。

    同 task_key 已有排队/在跑的 run 时按 coalesce 丢弃本次触发（等价旧
    ``coalesce=True, max_instances=1`` 的"积压即丢弃"语义）。
    """
    ensure_database_ready()
    task_run = TaskQueueService.enqueue(
        task_key=job_def.task_key,
        trigger_type="scheduled",
        conflict="skip",
    )
    if task_run is None:
        logger.info(
            "定时任务已在队列或执行中，本次触发按 coalesce 丢弃 task_key={}",
            job_def.task_key,
        )
    return task_run


def submit_manual_job(
    job_def: JobDefinition,
    params: dict[str, Any] | None = None,
) -> BackgroundTaskRun:
    """手动触发入队（202 语义），由 worker 进程领取执行，返回新建的 task_run。

    不再在当前进程起 daemon 线程执行——Web 进程只写队列，长任务不占请求线程。
    冲突时抛 ``TaskRunConflictError``；调用方负责把它映射为 HTTP 响应。
    """
    ensure_database_ready()
    try:
        return TaskQueueService.enqueue(
            task_key=job_def.task_key,
            trigger_type="manual",
            params=params,
            conflict="raise",
        )
    except TaskQueueConflictError as exc:
        blocking_task_run = (
            BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == exc.blocking_task_run_id)
            if exc.blocking_task_run_id is not None
            else None
        )
        if blocking_task_run is None:
            raise
        raise TaskRunConflictError(blocking_task_run) from exc


def _schedule_bootstrap_job(
    scheduler: BlockingScheduler,
    job_key: str,
    *,
    job_id: str,
    params: dict[str, Any] | None = None,
) -> None:
    """把一次性引导任务挂成立刻入队的 date job。

    复用注册表里 cron 任务的 task_key 与队列 mutex，因此与定时触发天然互斥；
    参数统一交给任务自己的 handler 或参数执行体。
    """
    if job_key not in BOOTSTRAP_QUEUE_TASK_KEYS:
        raise ValueError(f"unsupported_bootstrap_task_key: {job_key}")
    job_def = JOB_REGISTRY_BY_KEY.get(job_key)
    if job_def is None:
        logger.warning("引导任务未在注册表中，跳过 job_key={}", job_key)
        return
    retry_job_id = f"{job_id}_completion_guard"

    def _remove_completion_guard() -> None:
        if scheduler.get_job(retry_job_id) is not None:
            scheduler.remove_job(retry_job_id)

    def _schedule_short_retry(blocking_task_run_id: int | None) -> None:
        # one-shot date job 的瞬时故障不能让引导永久消失；重试间隔固定且只限两个 key。
        _remove_completion_guard()
        scheduler.add_job(
            _runner,
            args=[blocking_task_run_id],
            trigger="date",
            run_date=runtime_now() + timedelta(seconds=BOOTSTRAP_ERROR_RETRY_SECONDS),
            id=retry_job_id,
            replace_existing=True,
            misfire_grace_time=None,
        )

    def _schedule_completion_guard(task_run: BackgroundTaskRun) -> None:
        current_db_time = utc_now_for_db()
        if task_run.state == "running" and task_run.lease_expires_at is not None:
            target_db_time = task_run.lease_expires_at + timedelta(
                seconds=BOOTSTRAP_RETRY_GRACE_SECONDS
            )
        else:
            # pending 尚未发放 lease；给 worker 一个完整 lease 周期完成领取与首次执行。
            target_db_time = current_db_time + timedelta(
                seconds=DEFAULT_LEASE_SECONDS + BOOTSTRAP_RETRY_GRACE_SECONDS
            )
        delay_seconds = max(
            (target_db_time - current_db_time).total_seconds(),
            BOOTSTRAP_RETRY_GRACE_SECONDS,
        )
        _remove_completion_guard()
        scheduler.add_job(
            _runner,
            args=[task_run.id],
            trigger="date",
            run_date=runtime_now() + timedelta(seconds=delay_seconds),
            id=retry_job_id,
            replace_existing=True,
            misfire_grace_time=None,
        )

    def _enqueue() -> None:
        try:
            task_run = TaskQueueService.enqueue(
                task_key=job_def.task_key,
                trigger_type="startup",
                params=params,
                conflict="raise",
            )
        except TaskQueueConflictError as exc:
            if exc.blocking_task_run_id is None:
                # 冲突行恰在异常映射前释放 mutex，短延迟后重新走唯一约束裁决。
                _schedule_short_retry(None)
                return
            _follow_blocker(exc.blocking_task_run_id)
            return
        if task_run is not None:
            _schedule_completion_guard(task_run)

    def _follow_blocker(blocking_task_run_id: int) -> None:
        blocker = TaskQueueService.settle_bootstrap_blocker(
            task_key=job_def.task_key,
            task_run_id=blocking_task_run_id,
        )
        if blocker is None:
            _enqueue()
            return
        if blocker.state == "completed":
            _remove_completion_guard()
            return
        if blocker.state == "failed":
            failure_code = (blocker.result_summary or {}).get(
                INTERNAL_FAILURE_CODE_KEY
            )
            if (
                blocker.trigger_type == "startup"
                and failure_code != FAILURE_CODE_QUEUE_LEASE_EXPIRED
            ):
                # 业务失败保持原有单次执行语义；完成守卫只补偿进程/租约中断。
                _remove_completion_guard()
                return
            _enqueue()
            return
        if blocker.state in {"pending", "running"}:
            _schedule_completion_guard(blocker)
            return
        _enqueue()

    def _runner(blocking_task_run_id: int | None = None) -> None:
        try:
            ensure_database_ready()
            if blocking_task_run_id is None:
                _enqueue()
            else:
                _follow_blocker(blocking_task_run_id)
        except Exception:
            logger.exception("Bootstrap job enqueue failed job_key={}", job_key)
            try:
                _schedule_short_retry(blocking_task_run_id)
            except Exception:
                logger.exception("Bootstrap job retry scheduling failed job_key={}", job_key)

    # misfire_grace_time=None：避免默认 1s 宽限期把这次"立刻执行"的 date job
    # 在启动瞬时忙碌时静默判成 missed 而丢弃。
    scheduler.add_job(
        _runner,
        trigger="date",
        id=job_id,
        replace_existing=True,
        misfire_grace_time=None,
    )


def _bootstrap_gfriends_filetree_refresh(scheduler: BlockingScheduler) -> None:
    """首次部署/缓存缺失时的引导：立刻拉一次 GFriends Filetree 到 disk cache。

    - 目的：避免首个 JavDB 详情请求触发同步网络阻塞
    - 触发条件：disk cache 不存在或已超过 TTL（等 cron 又要一周太久）
    - 任何异常都吞掉：GFriends 只是头像美化，不能让引导逻辑打崩 APS 启动
    """
    try:
        from pathlib import Path

        cache_path = Path(settings.metadata.gfriends_filetree_cache_path).expanduser()
        if cache_path.exists():
            age_seconds = time.time() - cache_path.stat().st_mtime
            ttl_seconds = max(settings.metadata.gfriends_filetree_cache_ttl_hours, 1) * 3600
            if age_seconds <= ttl_seconds:
                # cache 仍新鲜，业务侧秒读，等 cron 定期刷新即可
                return

        logger.info("首次部署检测：GFriends Filetree 缓存缺失或过期，安排一次预热拉取")
        _schedule_bootstrap_job(
            scheduler,
            "gfriends_filetree_refresh",
            job_id="bootstrap_gfriends_filetree_refresh",
            # 启动预热尊重现有 TTL，不强制刷新；cron 的空参数由 handler 默认 force=True。
            params={"force": False},
        )
    except Exception:
        logger.exception("Skip bootstrap gfriends filetree refresh due to unexpected error")


def _bootstrap_movie_similarity_index(scheduler: BlockingScheduler) -> None:
    """相似度 alias 缺失时立刻安排首次构建，不阻塞 APS 启动。"""
    try:
        from src.service.discovery.qdrant_movie_similarity_store import (
            MovieSimilarityIndexError,
            get_qdrant_movie_similarity_store,
        )

        try:
            if get_qdrant_movie_similarity_store().is_ready():
                return
        except MovieSimilarityIndexError as exc:
            # Qdrant 暂时不可达时仍安排一次启动任务，让失败进入统一任务记录。
            logger.warning("检查影片相似度索引失败，仍安排启动构建 detail={}", exc)

        logger.info("影片相似度索引尚未就绪，安排一次启动构建")
        _schedule_bootstrap_job(
            scheduler,
            "movie_similarity_recompute",
            job_id="bootstrap_movie_similarity_index",
        )
    except Exception:
        logger.exception("Skip bootstrap movie similarity index due to unexpected error")


def build_scheduler() -> BlockingScheduler:
    timezone = get_runtime_timezone()
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(4)},
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone=timezone,
    )
    for job_def in JOB_REGISTRY:
        if job_def.manual_only:
            continue
        cron_expr = resolve_job_cron_expr(job_def)
        # cron 触发只入队（enqueue_scheduled_job），实际执行在 TaskWorker；
        # 入队秒级完成，APS 线程池不再被长任务占用。
        scheduler.add_job(
            enqueue_scheduled_job,
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
    scheduler = build_scheduler()
    _bootstrap_gfriends_filetree_refresh(scheduler)
    _bootstrap_movie_similarity_index(scheduler)
    # 队列 worker 与调度器同进程：APS 只按 cron 入队，worker 领取执行。
    worker = TaskWorker()
    worker.start()
    cron_info = " ".join(
        f"{get_job_cron_setting(j)}={resolve_job_cron_expr(j)}"
        for j in JOB_REGISTRY
        if not j.manual_only
    )
    logger.info("Starting scheduler runtime_timezone={} {}", get_runtime_timezone_name(), cron_info)
    scheduler.start()
