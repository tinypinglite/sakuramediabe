"""读取插件自己的 i18n 语言包。"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

_I18N_DIR = Path(__file__).resolve().parent / "i18n"
_LOCALES = ("zh-CN", "en")


@cache
def _catalog(locale: str) -> dict[str, str]:
    path = _I18N_DIR / f"{locale}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def text(key: str, locale: str, default: str) -> str:
    value = _catalog(locale).get(key)
    return value if isinstance(value, str) else default


def text_map(key: str) -> dict[str, Any]:
    return {
        locale: value
        for locale in _LOCALES
        if isinstance((value := _catalog(locale).get(key)), str)
    }
