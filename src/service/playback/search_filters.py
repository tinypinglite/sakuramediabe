"""播放域列表的关键词过滤表达式。

`/media-clips` 与 `/media-points` 的 `keyword` 共用同一套条件组装：
每个词在「番号表达式」与调用方给的文本条件上 OR，词与词之间由调用方 AND。
番号容忍度与影片搜索 `MovieService._number_search_target` 一致（大小写、`-/_`
分隔、FC2PPV 折叠），但这里只做过滤，不带相关度排序。
"""

import operator
import re
from collections.abc import Sequence
from functools import reduce

import peewee
from peewee import fn


def normalized_number_contains(column, term: str):
    """番号列的子串匹配表达式；词里没有任何 ASCII 字母数字时返回 None。"""
    normalized = term.strip().upper()
    if not any(char.isascii() and char.isalnum() for char in normalized):
        return None
    if re.fullmatch(r"\d+[-_]\d+", normalized):
        return column.contains(normalized)
    key = normalized.replace("-", "").replace("_", "")
    if key.startswith("FC2PPV"):
        key = "FC2" + key[len("FC2PPV") :]
    expression = fn.REPLACE(
        fn.UPPER(fn.TRANSLATE(column, "-_", "")), "FC2PPV", "FC2"
    )
    return expression.contains(key)


def keyword_conditions(
    terms: Sequence[str],
    *,
    number_column=None,
    text_condition_builder=None,
) -> list:
    """检索词 -> AND 条件列表；无词返回空列表。

    `number_column` 为 None 表示该类型不按番号匹配（如视频时刻）；
    `text_condition_builder(term)` 返回该词在文本字段上的 OR 条件（可为空列表）。
    某个词所有可匹配字段都不适用时返回恒假条件，避免该词被静默忽略成「匹配全部」。
    """
    conditions = []
    for term in terms:
        term_conditions = []
        if number_column is not None:
            number_condition = normalized_number_contains(number_column, term)
            if number_condition is not None:
                term_conditions.append(number_condition)
        if text_condition_builder is not None:
            term_conditions.extend(text_condition_builder(term))
        if not term_conditions:
            conditions.append(peewee.SQL("FALSE"))
        elif len(term_conditions) == 1:
            conditions.append(term_conditions[0])
        else:
            conditions.append(reduce(operator.or_, term_conditions))
    return conditions
