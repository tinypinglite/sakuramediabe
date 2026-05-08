from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable


class BackgroundTaskRunner:
    """进程内后台任务运行器，统一管理前端触发的长任务 future。"""

    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="background-task")
    _futures: dict[int, Future] = {}
    _lock = Lock()

    @classmethod
    def submit(cls, task_run_id: int, fn: Callable, *args, **kwargs) -> Future:
        future = cls._executor.submit(fn, *args, **kwargs)
        with cls._lock:
            cls._futures[task_run_id] = future
        future.add_done_callback(lambda completed_future: cls._cleanup(task_run_id, completed_future))
        return future

    @classmethod
    def has_active_task(cls, task_run_id: int) -> bool:
        with cls._lock:
            future = cls._futures.get(task_run_id)
            if future is None:
                return False
            if future.done():
                cls._futures.pop(task_run_id, None)
                return False
            return True

    @classmethod
    def _cleanup(cls, task_run_id: int, future: Future) -> None:
        with cls._lock:
            current_future = cls._futures.get(task_run_id)
            if current_future is future:
                cls._futures.pop(task_run_id, None)
