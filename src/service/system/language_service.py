"""用户/后台任务语言偏好。

请求路径的语言来自中间件 + 认证依赖；后台任务没有请求上下文，在任务执行作用域
内显式读取单账号的 ``User.language``，结束后复位，避免线程复用导致语言串台。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    LanguageState,
    current_language_state,
    normalize_language,
    reset_language_state,
    set_language_state,
)


def background_language() -> str:
    from src.model import User

    user = User.select(User.language).order_by(User.id).first()
    if user is None:
        return DEFAULT_LANGUAGE
    return normalize_language(user.language) or DEFAULT_LANGUAGE


@contextmanager
def task_language_scope() -> Iterator[None]:
    state = LanguageState(user_language=background_language())
    token = set_language_state(state)
    try:
        yield
    finally:
        reset_language_state(token)


@contextmanager
def language_scope_if_needed() -> Iterator[None]:
    """请求/任务已有语言上下文时复用；否则补一个后台语言作用域。"""
    if current_language_state() is not None:
        yield
        return
    with task_language_scope():
        yield
