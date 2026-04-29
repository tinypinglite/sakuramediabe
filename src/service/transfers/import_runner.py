from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

from src.common.database import ensure_database_ready


class DownloadImportRunner:
    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download-import")
    _futures: dict[int, Future] = {}
    _lock = Lock()

    @classmethod
    def submit(cls, import_job_id: int, fn: Callable, *args, **kwargs):
        future = cls._executor.submit(fn, *args, **kwargs)
        with cls._lock:
            cls._futures[import_job_id] = future
        future.add_done_callback(lambda completed_future: cls._cleanup(import_job_id, completed_future))
        return future

    @classmethod
    def has_active_job(cls, import_job_id: int) -> bool:
        with cls._lock:
            future = cls._futures.get(import_job_id)
            if future is None:
                return False
            if future.done():
                cls._futures.pop(import_job_id, None)
                return False
            return True

    @classmethod
    def _cleanup(cls, import_job_id: int, future: Future) -> None:
        with cls._lock:
            current_future = cls._futures.get(import_job_id)
            if current_future is future:
                cls._futures.pop(import_job_id, None)
