"""JSON 语言包加载与翻译。

语言包是纯数据文件，key 使用点分命名空间（如 ``errors.job_not_found``、
``tasks.movie_stats_sync.name``）。这里只做查表、复数选择和插值，
不引入 gettext/Babel 的编译流程。

约定：查表用的 catalog key 参数一律叫 ``catalog_key``，把 ``key`` 留给模板
占位符（插件语言包错误明细要插值插件自己的 key）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from loguru import logger

from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    FALLBACK_LANGUAGE,
    current_language,
    normalize_language,
)

CATALOG_DIR = Path(__file__).with_name("catalogs")

# 缺译文时的默认返回值标记；区别于显式传入的 None（表示"没有译文"）。
_MISSING = object()


@cache
def _load_catalog(language: str) -> dict[str, Any]:
    path = CATALOG_DIR / f"{language}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("i18n catalog missing: {}", path)
        return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("i18n catalog invalid: {}", path)
        return {}
    if not isinstance(data, dict):
        logger.error("i18n catalog root must be an object: {}", path)
        return {}
    return data


def _select_value(value: Any, params: dict[str, Any]) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if params.get("count") == 1:
            selected = value.get("one", value.get("other"))
        else:
            selected = value.get("other", value.get("one"))
        return selected if isinstance(selected, str) else None
    return None


def translate_from_catalogs(
    catalogs: Mapping[str, Mapping[str, Any]],
    catalog_key: str,
    *,
    locale: str | None = None,
    default: str | None = None,
    fallback_to_default: bool = True,
    fallback_language: str | None = DEFAULT_LANGUAGE,
    missing: Any = _MISSING,
    **params: Any,
) -> str | None:
    """从一组已加载的 catalog 渲染 key。

    供宿主全局 catalog 与插件自带 catalog 共用；catalog 的 key 是平铺点分
    字符串，值可以是普通模板或 ``{"one": ..., "other": ...}``。

    ``fallback_language`` 是缺译文时的回退语言；``missing`` 是缺译文（或插值
    失败）时的返回值，默认返回 key 本身。
    """
    resolved = normalize_language(locale) or current_language()
    value = catalogs.get(resolved, {}).get(catalog_key)
    if value is None and fallback_to_default and resolved != DEFAULT_LANGUAGE:
        value = (
            catalogs.get(fallback_language, {}).get(catalog_key)
            if fallback_language
            else None
        )
    selected = _select_value(value, params)
    if selected is None:
        if default is not None:
            return default
        return catalog_key if missing is _MISSING else missing
    try:
        return selected.format(**params)
    except (KeyError, IndexError, ValueError):
        logger.warning(
            "i18n interpolation failed key={} language={}", catalog_key, resolved
        )
        if default is not None:
            return default
        return selected if missing is _MISSING else missing


def translate(
    catalog_key: str,
    *,
    locale: str | None = None,
    default: str | None = None,
    fallback_to_default: bool = True,
    **params: Any,
) -> str:
    """按当前/指定语言渲染宿主 catalog 的 key。

    ``locale`` 是目标语言参数，刻意不叫 ``language``，避免和消息里的
    ``{language}`` 占位符冲突；catalog key 本身叫 ``catalog_key``，避免和
    消息里的 ``{key}`` 占位符冲突（插件语言包错误明细要插值插件自己的 key）。
    - 缺失译文时默认回退到 zh-CN；错误文案会传 ``fallback_to_default=False``，
      由调用方决定英文通用兜底，避免把中文透给英文界面。
    - 插值失败不抛异常，返回 default 或原始模板，保证错误 handler 不会二次 500。
    """
    resolved = normalize_language(locale) or current_language()
    catalogs: dict[str, Mapping[str, Any]] = {resolved: _load_catalog(resolved)}
    if fallback_to_default and resolved != DEFAULT_LANGUAGE:
        catalogs[DEFAULT_LANGUAGE] = _load_catalog(DEFAULT_LANGUAGE)
    rendered = translate_from_catalogs(
        catalogs,
        catalog_key,
        locale=resolved,
        default=default,
        fallback_to_default=fallback_to_default,
        missing=catalog_key,
        **params,
    )
    return catalog_key if rendered is None else rendered


def translate_for_display(
    catalog_key: str,
    *,
    locale: str | None = None,
    default: str | None = None,
    **params: Any,
) -> str | None:
    """渲染面向界面的文案，不把中文回退给非中文界面。

    - 当前 locale 优先；
    - 非 ``DEFAULT_LANGUAGE`` 时缺失回退 en catalog；
    - 仍缺失或插值失败返回 ``default``（可能为 None），由调用方决定兜底。

    这与 :func:`translate` 的区别是：后者为了任务/通知的源文案兼容会回退 zh-CN，
    而展示层在英文/未来语言下出现中文比出现英文更糟。
    """
    # 显式传入的未知 locale 走英文兜底；未传 locale 才取请求上下文。
    resolved = (
        current_language()
        if locale is None
        else normalize_language(locale) or FALLBACK_LANGUAGE
    )
    catalogs: dict[str, Mapping[str, Any]] = {
        resolved: _load_catalog(resolved),
        FALLBACK_LANGUAGE: _load_catalog(FALLBACK_LANGUAGE),
    }
    return translate_from_catalogs(
        catalogs,
        catalog_key,
        locale=resolved,
        default=default,
        fallback_language=FALLBACK_LANGUAGE,
        missing=None,
        **params,
    )
