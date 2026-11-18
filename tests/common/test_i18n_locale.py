import pytest

from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    LanguageState,
    apply_user_language,
    current_language,
    normalize_language,
    parse_accept_language,
    reset_language_state,
    set_language_state,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zh", "zh-CN"),
        ("zh-CN", "zh-CN"),
        ("zh_CN", "zh-CN"),
        ("zh-Hans", "zh-CN"),
        ("zh-SG", "zh-CN"),
        ("en", "en"),
        ("en-US", "en"),
        ("EN-gb", "en"),
        ("zh-TW", None),
        ("zh-Hant", None),
        ("fr", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


def test_parse_accept_language_prefers_highest_quality():
    assert parse_accept_language("fr;q=0.9,en;q=0.8,zh-CN;q=0.7") == "en"


def test_parse_accept_language_keeps_header_order_for_equal_quality():
    assert parse_accept_language("en;q=0.8,zh-CN;q=0.8") == "en"
    assert parse_accept_language("zh-CN;q=0.8,en;q=0.8") == "zh-CN"


def test_parse_accept_language_ignores_unsupported_and_zero_quality():
    assert parse_accept_language("zh-TW,fr;q=0.8,en;q=0") is None
    assert parse_accept_language("zh-TW,fr;q=0.8,en;q=0.1") == "en"


def test_language_state_priority_header_then_user_then_default():
    state = LanguageState()
    assert state.effective == DEFAULT_LANGUAGE
    state.user_language = "en"
    assert state.effective == "en"
    state.header_language = "zh-CN"
    assert state.effective == "zh-CN"


def test_apply_user_language_mutates_current_request_state():
    token = set_language_state(LanguageState(header_language="zh-CN"))
    try:
        assert current_language() == "zh-CN"
        apply_user_language("en")
        # 请求头优先；用户偏好不会覆盖显式 header。
        assert current_language() == "zh-CN"
    finally:
        reset_language_state(token)


def test_apply_user_language_is_used_without_header():
    token = set_language_state(LanguageState())
    try:
        apply_user_language("en-US")
        assert current_language() == "en"
    finally:
        reset_language_state(token)


def test_current_language_defaults_without_request_state():
    assert current_language() == DEFAULT_LANGUAGE
