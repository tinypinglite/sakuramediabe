from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Callable

from loguru import logger

from src.config.config import settings
from src.model import get_database, init_database


def ensure_database_ready():
    try:
        database = get_database()
        logger.debug("Transfer worker database proxy already initialized")
    except RuntimeError:
        logger.info("Transfer worker database proxy not initialized, initializing from settings")
        database = init_database(settings.database)
    if database.is_closed():
        logger.info("Transfer worker database is closed, connecting now")
        database.connect()
    return database


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
