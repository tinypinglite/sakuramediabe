"""系统播放列表名的读时本地化。

系统列表（收藏 / 最近播放）的名字由 ``initdb`` 以中文预置进数据库；这里按
请求语言在出参时替换成对应语言的名字，库里的原值保持不变——保留名校验、
``playlist.name`` 唯一索引和既有数据都不受影响。

键用 ``kind`` 而不是库里的名字：``initdb`` 遇到同名用户列表会给系统列表追加
序号（"收藏 2"），``kind`` 才是稳定标识。自定义列表的名字是用户数据，不翻译。
"""

from __future__ import annotations

from src.i18n.catalog import translate
from src.i18n.locale import SUPPORTED_LANGUAGES
from src.model import (
    FAVORITES_PLAYLIST_NAME,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    RECENTLY_PLAYED_PLAYLIST_NAME,
)

SYSTEM_PLAYLIST_NAME_KEYS = {
    PLAYLIST_KIND_FAVORITES: "playlists.system.favorites.name",
    PLAYLIST_KIND_RECENTLY_PLAYED: "playlists.system.recently_played.name",
}

SYSTEM_PLAYLIST_DESCRIPTION_KEYS = {
    PLAYLIST_KIND_FAVORITES: "playlists.system.favorites.description",
    PLAYLIST_KIND_RECENTLY_PLAYED: "playlists.system.recently_played.description",
}


def localize_system_playlist_name(
    kind: str,
    fallback: str,
    locale: str | None = None,
) -> str:
    """系统列表展示名；非系统 kind 原样返回。"""
    catalog_key = SYSTEM_PLAYLIST_NAME_KEYS.get(kind)
    if catalog_key is None:
        return fallback
    return translate(catalog_key, locale=locale, default=fallback)


def localize_system_playlist_description(
    kind: str,
    fallback: str,
    locale: str | None = None,
) -> str:
    """系统列表描述；非系统 kind 原样返回。"""
    catalog_key = SYSTEM_PLAYLIST_DESCRIPTION_KEYS.get(kind)
    if catalog_key is None:
        return fallback
    return translate(catalog_key, locale=locale, default=fallback)


def system_playlist_reserved_names() -> set[str]:
    """系统列表在各语言下的名字（含库里的中文原名）。

    用户自定义列表不能占用这些名字，否则英文界面会出现两个都叫 "Favorites"
    的列表。
    """
    names = {FAVORITES_PLAYLIST_NAME, RECENTLY_PLAYED_PLAYLIST_NAME}
    for catalog_key in SYSTEM_PLAYLIST_NAME_KEYS.values():
        for language in SUPPORTED_LANGUAGES:
            resolved = translate(catalog_key, locale=language, default="")
            if resolved:
                names.add(resolved)
    return names
