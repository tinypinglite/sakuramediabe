"""任务队列 worker（任务架构 Wave 1）：领取并执行队列托管的 task_run。

跑在 APS 进程内：N 条领取线程 + 1 条 housekeeper 线程。
- 领取线程：``claim_next`` 拿到行后按注册表解析 executor（Wave 1 即 service_factory），
  经 ``ActivityService.run_task(task_run_id=...)`` 复用同一行执行；
- housekeeper：为在飞行的 run 续租；回收租约过期的队列行；顺带用 owner_pid 判活
  回收遗留的非队列行（CLI 直跑/导入族崩溃残留），两类回收都联动业务恢复钩子。
"""

from __future__ import annotations

import threading

from loguru import logger

from src.common.database import ensure_database_ready
from src.model import BackgroundTaskRun
from src.scheduler.queue_tasks import (
    LANE_CONCURRENCY,
    LANE_DEFAULT,
    NON_DEFAULT_LANE_TASK_KEYS,
    QUEUE_TASK_REGISTRY,
    lane_task_keys,
)
from src.scheduler.registry import JOB_REGISTRY_BY_KEY
from src.service.system import ActivityService
from src.service.system.task_queue_service import (
    DEFAULT_LEASE_SECONDS,
    TaskQueueService,
)

CLAIM_POLL_INTERVAL_SECONDS = 1.0
LEGACY_RECOVERY_ERROR_MESSAGE = "任务执行进程已退出，任务按中断回收"


class TaskWorker:
    def __init__(
        self,
        *,
        lanes: dict[str, int] | None = None,
        poll_interval: float = CLAIM_POLL_INTERVAL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        self._lanes = dict(lanes or LANE_CONCURRENCY)
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # run_id -> task_key，housekeeper 按这份注册表续租。
        self._in_flight: dict[int, str] = {}

    def start(self) -> None:
        for lane, concurrency in self._lanes.items():
            for index in range(concurrency):
                threading.Thread(
                    target=self._claim_loop,
                    args=(lane,),
                    name=f"task-worker-{lane}-{index}",
                    daemon=True,
                ).start()
        threading.Thread(
            target=self._housekeeping_loop,
            name="task-worker-housekeeper",
            daemon=True,
        ).start()
        logger.info(
            "Task worker started lanes={} lease_seconds={}",
            self._lanes,
            self._lease_seconds,
        )

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ claim

    def _claim_loop(self, lane: str = LANE_DEFAULT) -> None:
        ensure_database_ready()
        # default 道排除专属道任务；专属道只领取本道任务。
        include_task_keys = None if lane == LANE_DEFAULT else lane_task_keys(lane)
        exclude_task_keys = NON_DEFAULT_LANE_TASK_KEYS if lane == LANE_DEFAULT else None
        while not self._stop.is_set():
            try:
                task_run = TaskQueueService.claim_next(
                    lease_seconds=self._lease_seconds,
                    include_task_keys=include_task_keys,
                    exclude_task_keys=exclude_task_keys,
                )
            except Exception:
                logger.exception("Task worker claim failed")
                self._stop.wait(self._poll_interval * 5)
                continue
            if task_run is None:
                self._stop.wait(self._poll_interval)
                continue
            self._execute(task_run)

    def _execute(self, task_run: BackgroundTaskRun) -> None:
        params = task_run.params if isinstance(task_run.params, dict) else {}
        queue_def = QUEUE_TASK_REGISTRY.get(task_run.task_key)
        job_def = JOB_REGISTRY_BY_KEY.get(task_run.task_key)
        # 解析顺序：队列专属任务（key 不在 cron 注册表，或运行带 params 的单资源任务）
        # 优先；否则回落 cron 批任务的 service_factory。
        if queue_def is not None and (job_def is None or params):
            func = lambda reporter: queue_def.handler(reporter, params)
            log_name = queue_def.log_name
            notify_result = queue_def.notify_result
        elif job_def is not None and params and job_def.params_handler is not None:
            # 插件/参数化任务：手动带参执行体，与 cron 批任务共用互斥与恢复。
            func = lambda reporter: job_def.params_handler(reporter, params)
            log_name = job_def.log_name
            notify_result = True
        elif job_def is not None and job_def.service_factory is not None:
            func = job_def.service_factory
            log_name = job_def.log_name
            notify_result = True
        else:
            # 队列里出现未注册 key（如插件停用后的残留行）：显式失败，避免无限重领。
            ActivityService.fail_task_run(
                task_run.id,
                error_message=f"task_key 未在注册表中: {task_run.task_key}",
            )
            return
        with self._lock:
            self._in_flight[task_run.id] = task_run.task_key
        try:
            ActivityService.run_task(
                task_key=task_run.task_key,
                trigger_type=task_run.trigger_type,
                func=func,
                task_run_id=task_run.id,
                log_task_name=log_name,
                notify_result=notify_result,
            )
        except Exception:
            # run_task 内部已把异常写入 task_run；这里只记 worker 层日志。
            logger.exception(
                "Task worker run crashed task_key={} task_run_id={}",
                task_run.task_key,
                task_run.id,
            )
        finally:
            with self._lock:
                self._in_flight.pop(task_run.id, None)

    # ----------------------------------------------------------- housekeeping

    def _housekeeping_loop(self) -> None:
        ensure_database_ready()
        interval = max(self._lease_seconds // 3, 5)
        while not self._stop.wait(interval):
            try:
                self._renew_in_flight_leases()
                self._recover_interrupted()
            except Exception:
                logger.exception("Task worker housekeeping failed")

    def _renew_in_flight_leases(self) -> None:
        with self._lock:
            in_flight_ids = list(self._in_flight)
        if in_flight_ids:
            TaskQueueService.renew_leases(in_flight_ids, lease_seconds=self._lease_seconds)

    def _recover_interrupted(self) -> None:
        # 队列行：租约过期即回收。遗留非队列行（CLI 直跑/导入族）：owner_pid 判活回收，
        # 让 stale mutex 不再需要等下一次同 key 触发时的 _prepare_recovery 才解开。
        recovered = list(TaskQueueService.recover_expired_leases())
        recovered.extend(
            ActivityService.recover_interrupted_task_runs(
                error_message=LEGACY_RECOVERY_ERROR_MESSAGE,
            )
        )
        if not recovered:
            return
        self._run_business_recovery({run.task_key for run in recovered})

    @staticmethod
    def _run_business_recovery(task_keys: set[str]) -> None:
        # 复用启动恢复的业务钩子注册表，保证崩溃回收与启动回收的领域联动一致。
        from src.start.recovery import BUSINESS_RECOVERY_HANDLERS

        for task_key in sorted(task_keys):
            handler = BUSINESS_RECOVERY_HANDLERS.get(task_key)
            if handler is None:
                continue
            try:
                stats = handler()
                logger.info(
                    "Task worker business recovery task_key={} stats={}", task_key, stats
                )
            except Exception:
                logger.exception(
                    "Task worker business recovery failed task_key={}", task_key
                )
