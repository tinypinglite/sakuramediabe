import time
from typing import Any, Callable

from loguru import logger

from src.scheduler.logging import get_task_logger


def run_logged_task(task_name: str, func: Callable[[], Any]) -> Any:
    task_logger = get_task_logger(task_name)
    with logger.contextualize(task=task_name):
        started_at = time.time()
        task_logger.info("Scheduler task started")
        try:
            result = func()
        except Exception:
            elapsed_ms = int((time.time() - started_at) * 1000)
            task_logger.exception("Scheduler task failed elapsed_ms={}", elapsed_ms)
            raise
        elapsed_ms = int((time.time() - started_at) * 1000)
        task_logger.info("Scheduler task finished elapsed_ms={} result={}", elapsed_ms, result)
        return result
