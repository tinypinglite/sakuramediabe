from pathlib import Path
from typing import Dict, Tuple

from loguru import logger

from src.common.logging import get_logging_level_name
from src.config.config import settings

_TASK_SINKS: Dict[Tuple[str, str], int] = {}
_TASK_LEVELS: Dict[Tuple[str, str], str] = {}


def get_task_logger(task_name: str):
    log_dir = Path(settings.scheduler.log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = (Path.cwd() / log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    sink_key = (str(log_dir), task_name)
    level_name = get_logging_level_name()
    if sink_key in _TASK_SINKS and _TASK_LEVELS.get(sink_key) != level_name:
        logger.remove(_TASK_SINKS[sink_key])
        del _TASK_SINKS[sink_key]
        del _TASK_LEVELS[sink_key]

    if sink_key not in _TASK_SINKS:
        log_path = log_dir / f"{task_name}.log"
        _TASK_SINKS[sink_key] = logger.add(
            log_path,
            level=level_name,
            rotation="100 MB",
            retention=3,
            compression="gz",
            filter=lambda record: record.get("extra", {}).get("task") == task_name,
            enqueue=False,
        )
        _TASK_LEVELS[sink_key] = level_name
    return logger.bind(task=task_name)
