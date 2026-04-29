import logging
import sys

from loguru import logger

from src.config.config import settings

_LOGURU_STDERR_SINK_ID: int | None = None
_DEFAULT_LOGURU_SINK_REMOVED = False
_MANAGED_LOGGER_NAMES = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "peewee",
)
_QUIET_LOGGER_LEVELS = {
    "httpx": "WARNING",
    "httpcore": "WARNING",
}
_SUPPORTED_LOG_LEVELS = {
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
}
_LOG_LEVEL_ALIASES = {
    "WARN": "WARNING",
    "FATAL": "CRITICAL",
}


def get_logging_level_name() -> str:
    configured_level = str(settings.logging.level).strip().upper()
    normalized_level = _LOG_LEVEL_ALIASES.get(configured_level, configured_level)
    if normalized_level not in _SUPPORTED_LOG_LEVELS:
        raise ValueError(f"Unsupported log level: {settings.logging.level}")
    return normalized_level


def configure_logging() -> None:
    global _DEFAULT_LOGURU_SINK_REMOVED
    global _LOGURU_STDERR_SINK_ID

    level_name = get_logging_level_name()
    root_logger = logging.getLogger()
    # 每次都刷新 root stream handler，避免测试/CLI 多次调用后仍持有已关闭的 stderr。
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root_logger.addHandler(logging.StreamHandler())
    root_logger.setLevel(level_name)

    for logger_name in _MANAGED_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(level_name)
    for logger_name, logger_level in _QUIET_LOGGER_LEVELS.items():
        logging.getLogger(logger_name).setLevel(logger_level)

    if not _DEFAULT_LOGURU_SINK_REMOVED:
        try:
            logger.remove(0)
        except ValueError:
            pass
        _DEFAULT_LOGURU_SINK_REMOVED = True

    if _LOGURU_STDERR_SINK_ID is not None:
        try:
            logger.remove(_LOGURU_STDERR_SINK_ID)
        except ValueError:
            pass

    _LOGURU_STDERR_SINK_ID = logger.add(sys.stderr, level=level_name)
