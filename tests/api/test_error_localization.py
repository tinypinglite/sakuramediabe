from src.api.exception import exception as exception_module
from src.i18n import catalog as catalog_module


def _error(monkeypatch, locale: str, code: str, source: str, status: int) -> str:
    monkeypatch.setattr(exception_module, "current_language", lambda: locale)
    return exception_module._localized_error_message(code, source, status)


def test_default_language_keeps_source_message_for_unlisted_code(monkeypatch):
    assert (
        _error(monkeypatch, "zh-CN", "plugin_not_found", "插件不存在", 404)
        == "插件不存在"
    )


def test_default_language_uses_chinese_handler_catalog(monkeypatch):
    assert (
        _error(monkeypatch, "zh-CN", "validation_error", "Request validation failed", 422)
        == "请求参数校验失败"
    )
    assert _error(monkeypatch, "zh-CN", "unauthorized", "Unauthorized", 401) == "请先登录"
    assert (
        _error(monkeypatch, "zh-CN", "forbidden", "Forbidden", 403)
        == "没有权限执行此操作"
    )
    assert (
        _error(monkeypatch, "zh-CN", "internal_error", "Internal server error", 500)
        == "服务器内部错误"
    )


def test_english_uses_catalog(monkeypatch):
    assert (
        _error(monkeypatch, "en", "validation_error", "任意源文案", 422)
        == "Request validation failed"
    )
    assert (
        _error(monkeypatch, "en", "forbidden", "任意源文案", 403)
        == "You do not have permission to perform this action"
    )


def test_unsupported_locale_falls_back_to_english_not_chinese(monkeypatch):
    assert (
        _error(monkeypatch, "ja", "validation_error", "请求参数校验失败", 422)
        == "Request validation failed"
    )
    assert (
        _error(monkeypatch, "ja", "forbidden", "没有权限执行此操作", 403)
        == "You do not have permission to perform this action"
    )


def test_english_http_error_keeps_status_specific_message(monkeypatch):
    assert (
        _error(monkeypatch, "en", "http_error", "Not Found", 404)
        == "The requested resource was not found"
    )
    assert (
        _error(monkeypatch, "en", "http_error", "Boom", 500)
        == "Internal server error"
    )


def test_chinese_http_error_uses_generic_catalog(monkeypatch):
    assert _error(monkeypatch, "zh-CN", "http_error", "Not Found", 404) == "请求失败"


def test_unknown_code_and_status_uses_english_fallbacks(monkeypatch):
    assert (
        _error(monkeypatch, "en", "unknown_code", "中文文案", 503)
        == "Internal server error"
    )
    assert (
        _error(monkeypatch, "en", "unknown_code", "中文文案", 409)
        == "The request conflicts with the current state"
    )


def test_translate_for_display_never_falls_back_to_chinese(monkeypatch):
    monkeypatch.setattr(catalog_module, "current_language", lambda: "ja")
    assert (
        catalog_module.translate_for_display(
            "tasks.movie_stats_sync.name", locale="ja"
        )
        == "Movie stats and heat sync"
    )
    assert (
        catalog_module.translate_for_display(
            "missing.key", locale="zh-CN", default="源文案"
        )
        == "源文案"
    )
    assert catalog_module.translate_for_display("missing.key", locale="en") is None
