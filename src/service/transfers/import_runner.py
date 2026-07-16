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


class MediaRapidUploadRunner:
    """批量秒传专用线程池；批次内部串行，不与导入作业的 id 空间混用。"""

    _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-rapid-upload")
    _futures: dict[int, Future] = {}
    _lock = Lock()

    @classmethod
    def submit(cls, batch_id: int, fn: Callable, *args, **kwargs):
        future = cls._executor.submit(fn, *args, **kwargs)
        with cls._lock:
            cls._futures[batch_id] = future
        future.add_done_callback(
            lambda completed_future: cls._cleanup(batch_id, completed_future)
        )
        return future

    @classmethod
    def has_active_batch(cls, batch_id: int) -> bool:
        with cls._lock:
            future = cls._futures.get(batch_id)
            if future is None:
                return False
            if future.done():
                cls._futures.pop(batch_id, None)
                return False
            return True

    @classmethod
    def _cleanup(cls, batch_id: int, future: Future) -> None:
        with cls._lock:
            if cls._futures.get(batch_id) is future:
                cls._futures.pop(batch_id, None)
