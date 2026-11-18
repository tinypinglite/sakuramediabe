"""汉字姓名的简繁日字形变体，供目录搜索匹配使用。"""

from __future__ import annotations

import re
import threading

from opencc import OpenCC

_CONVERTERS = threading.local()
_HAN_PATTERN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")


def _get_converters() -> tuple[OpenCC, OpenCC, OpenCC, OpenCC]:
    converters = getattr(_CONVERTERS, "value", None)
    if converters is None:
        converters = (
            OpenCC("s2t"),
            OpenCC("t2jp"),
            OpenCC("jp2t"),
            OpenCC("t2s"),
        )
        _CONVERTERS.value = converters
    return converters


def han_script_variants(value: str) -> tuple[str, ...]:
    """返回原形、日文新字体、简体和繁体变体，保持顺序并去重。"""
    if not value or not _HAN_PATTERN.search(value):
        return (value,) if value else ()
    s2t, t2jp, jp2t, t2s = _get_converters()
    japanese_traditional = jp2t.convert(value)
    traditional = s2t.convert(japanese_traditional)
    variants = (
        value,
        t2jp.convert(traditional),
        t2s.convert(japanese_traditional),
        traditional,
    )
    return tuple(dict.fromkeys(variants))
