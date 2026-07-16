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
        lambda sink, **kwargs: added_sinks.append((sink, kwargs)) or 101,
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
        # 断言级别正确 + diagnose 已关闭（回归保护：不允许再打开 diagnose，避免异常
        # 回溯里的 cookies/密码等敏感局部变量被 loguru 明文打进日志）。
        assert added_sinks == [(sys.stderr, {"level": "DEBUG", "diagnose": False})]
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

    def fake_add(sink, **kwargs):
        sink_id = next_sink_id["value"]
        next_sink_id["value"] += 1
        added_sinks.append((sink, kwargs, sink_id))
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
        # 两次配置都要显式关闭 diagnose，等级切换不应“顺带”把它打开。
        assert added_sinks == [
            (sys.stderr, {"level": "INFO", "diagnose": False}, 200),
            (sys.stderr, {"level": "WARNING", "diagnose": False}, 201),
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


def test_configure_logging_does_not_leak_local_variables_in_exception_traceback(monkeypatch):
    """真实 loguru sink：异常回溯里不能出现调用栈上敏感局部变量的值。

    loguru diagnose=True 会把途经栈帧里的局部变量原样打进日志，本用例通过真实
    logger.exception() 调用验证 diagnose=False 生效——不是仅断言参数被传对了。
    """
    import io

    sink = io.StringIO()
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    original_sink_id = logging_module._LOGURU_STDERR_SINK_ID
    original_removed = logging_module._DEFAULT_LOGURU_SINK_REMOVED

    # 走真实 configure_logging 之后，直接把测试的 StringIO sink 挂上，参数与生产一致。
    monkeypatch.setattr(logging_module.settings.logging, "level", "INFO")
    logging_module.configure_logging()
    test_sink_id = logging_module.logger.add(sink, level="INFO", diagnose=False)

    try:
        cookies = "UID=1234567_A1_1700000000; CID=deadbeef; SEID=cafebabe"
        try:
            _ = cookies.upper()
            raise RuntimeError("boom")
        except Exception:
            logging_module.logger.exception("cloud115 call failed")

        output = sink.getvalue()
        # 消息本身应该在，但异常回溯不应显式打印 cookies 字符串值。
        assert "cloud115 call failed" in output
        assert "UID=1234567_A1_1700000000" not in output
        assert "deadbeef" not in output
        assert "cafebabe" not in output
    finally:
        logging_module.logger.remove(test_sink_id)
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        logging_module._LOGURU_STDERR_SINK_ID = original_sink_id
        logging_module._DEFAULT_LOGURU_SINK_REMOVED = original_removed
