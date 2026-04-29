import logging
import sys

import src.common.logging as logging_module


def test_configure_logging_sets_root_and_managed_logger_levels(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_root_level = root_logger.level
    managed_loggers = {
        name: logging.getLogger(name).level
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "peewee", "httpx", "httpcore")
    }
    added_sinks = []
    removed_sinks = []

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    monkeypatch.setattr(logging_module.settings.logging, "level", "debug")
    monkeypatch.setattr(logging_module, "_DEFAULT_LOGURU_SINK_REMOVED", False)
    monkeypatch.setattr(logging_module, "_LOGURU_STDERR_SINK_ID", None)
    monkeypatch.setattr(
        logging_module.logger,
        "remove",
        lambda sink_id=None: removed_sinks.append(sink_id),
    )
    monkeypatch.setattr(
        logging_module.logger,
        "add",
        lambda sink, level: added_sinks.append((sink, level)) or 101,
    )

    try:
        logging_module.configure_logging()

        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert logging.getLogger("uvicorn").level == logging.DEBUG
        assert logging.getLogger("uvicorn.error").level == logging.DEBUG
        assert logging.getLogger("uvicorn.access").level == logging.DEBUG
        assert logging.getLogger("peewee").level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert removed_sinks == [0]
        assert added_sinks == [(sys.stderr, "DEBUG")]
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_root_level)
        for name, level in managed_loggers.items():
            logging.getLogger(name).setLevel(level)


def test_configure_logging_refreshes_loguru_sink_without_duplicating_root_handler(monkeypatch):
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_root_level = root_logger.level
    added_sinks = []
    removed_sinks = []

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    monkeypatch.setattr(logging_module, "_DEFAULT_LOGURU_SINK_REMOVED", False)
    monkeypatch.setattr(logging_module, "_LOGURU_STDERR_SINK_ID", None)
    monkeypatch.setattr(
        logging_module.logger,
        "remove",
        lambda sink_id=None: removed_sinks.append(sink_id),
    )

    next_sink_id = {"value": 200}

    def fake_add(sink, level):
        sink_id = next_sink_id["value"]
        next_sink_id["value"] += 1
        added_sinks.append((sink, level, sink_id))
        return sink_id

    monkeypatch.setattr(logging_module.logger, "add", fake_add)

    try:
        monkeypatch.setattr(logging_module.settings.logging, "level", "INFO")
        logging_module.configure_logging()
        first_handler_count = len(root_logger.handlers)

        monkeypatch.setattr(logging_module.settings.logging, "level", "WARNING")
        logging_module.configure_logging()

        assert first_handler_count == 1
        assert len(root_logger.handlers) == 1
        assert removed_sinks == [0, 200]
        assert added_sinks == [
            (sys.stderr, "INFO", 200),
            (sys.stderr, "WARNING", 201),
        ]
        assert logging_module._LOGURU_STDERR_SINK_ID == 201
        assert root_logger.level == logging.WARNING
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_root_level)


def test_get_logging_level_name_rejects_invalid_value(monkeypatch):
    monkeypatch.setattr(logging_module.settings.logging, "level", "TRACE")

    try:
        logging_module.get_logging_level_name()
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Unsupported log level" in str(exc)
