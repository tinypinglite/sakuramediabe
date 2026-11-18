"""通用文本本地化：从 ``*_i18n`` 字典按当前/指定语言取文本。"""

from __future__ import annotations

from collections.abc import Mapping

from src.i18n.locale import DEFAULT_LANGUAGE, current_language, normalize_language


def resolve_text(
    default: str,
    translations: Mapping[str, str] | None,
    locale: str | None = None,
) -> str:
    """按当前语言 → zh-CN → default 的顺序解析静态文案。"""
    if not translations:
        return default
    resolved = normalize_language(locale) or current_language()
    return (
        translations.get(resolved)
        or translations.get(DEFAULT_LANGUAGE)
        or default
    )
