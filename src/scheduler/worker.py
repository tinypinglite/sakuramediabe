"""任务队列 worker（任务架构 Wave 1）：领取并执行队列托管的 task_run。

跑在 APS 进程内：N 条领取线程 + 1 条 housekeeper 线程。
- 领取线程：``claim_next`` 拿到行后按统一 JobDefinition 解析 executor，
  经 ``ActivityService.run_task(task_run_id=...)`` 复用同一行执行；
- housekeeper：为在飞行的 run 续租；回收租约过期的队列行并联动业务恢复钩子。
"""

from __future__ import annotations

import threading

from loguru import logger

from src.common.database import ensure_database_ready
from src.model import BackgroundTaskRun
from src.scheduler.contracts import JobExecutionError
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
        queue_def = QUEUE_TASK_REGISTRY.get(task_run.task_key)
        job_def = JOB_REGISTRY_BY_KEY.get(task_run.task_key)
        definition = job_def or queue_def
        try:
            if definition is None:
                raise JobExecutionError(f"task_key 未在注册表中: {task_run.task_key}")
            func = definition.build_executor(task_run.params)
        except JobExecutionError as exc:
            # 插件停用、持久参数与声明不匹配等情况明确失败，避免无限重领。
            ActivityService.fail_task_run(
                task_run.id,
                error_message=str(exc),
            )
            return
        with self._lock:
            self._in_flight[task_run.id] = task_run.task_key
        try:
            ActivityService.run_task(
                func=func,
                task_run_id=task_run.id,
                log_task_name=definition.log_name,
                notify_result=definition.notify_result,
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
                self._recover_expired_leases()
            except Exception:
                logger.exception("Task worker housekeeping failed")

    def _renew_in_flight_leases(self) -> None:
        with self._lock:
            in_flight_ids = list(self._in_flight)
        if in_flight_ids:
            TaskQueueService.renew_leases(in_flight_ids, lease_seconds=self._lease_seconds)

    def _recover_expired_leases(self) -> None:
        recovered = TaskQueueService.recover_expired_leases()
        if not recovered:
            return
        self._run_business_recovery({run.task_key for run in recovered})

    @staticmethod
    def _run_business_recovery(task_keys: set[str]) -> None:
        from src.start.recovery import recover_business_states

        recover_business_states(task_keys)
