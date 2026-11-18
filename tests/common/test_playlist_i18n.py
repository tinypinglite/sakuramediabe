"""系统播放列表名的读时本地化。"""

import json
from contextlib import contextmanager

from src.i18n.catalog import CATALOG_DIR
from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.i18n.playlist_names import (
    localize_system_playlist_description,
    localize_system_playlist_name,
    system_playlist_reserved_names,
)
from src.model import (
    FAVORITES_PLAYLIST_DESCRIPTION,
    FAVORITES_PLAYLIST_NAME,
    PLAYLIST_KIND_CUSTOM,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
    RECENTLY_PLAYED_PLAYLIST_NAME,
    Playlist,
)
from src.schema.collections.playlists import PlaylistResource
from src.schema.common.playlists import PlaylistSummaryResource


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def _load_catalog(filename: str) -> dict:
    return json.loads((CATALOG_DIR / filename).read_text(encoding="utf-8"))


def test_system_playlist_name_follows_request_language():
    with _language("en"):
        assert (
            localize_system_playlist_name(PLAYLIST_KIND_FAVORITES, FAVORITES_PLAYLIST_NAME)
            == "Favorites"
        )
        assert (
            localize_system_playlist_name(
                PLAYLIST_KIND_RECENTLY_PLAYED,
                RECENTLY_PLAYED_PLAYLIST_NAME,
            )
            == "Recently Played"
        )

    with _language("zh-CN"):
        assert (
            localize_system_playlist_name(PLAYLIST_KIND_FAVORITES, FAVORITES_PLAYLIST_NAME)
            == FAVORITES_PLAYLIST_NAME
        )
        assert (
            localize_system_playlist_name(
                PLAYLIST_KIND_RECENTLY_PLAYED,
                RECENTLY_PLAYED_PLAYLIST_NAME,
            )
            == RECENTLY_PLAYED_PLAYLIST_NAME
        )


def test_system_playlist_description_follows_request_language():
    with _language("en"):
        assert (
            localize_system_playlist_description(
                PLAYLIST_KIND_FAVORITES,
                FAVORITES_PLAYLIST_DESCRIPTION,
            )
            == "Movies you have favorited manually"
        )

    with _language("zh-CN"):
        assert (
            localize_system_playlist_description(
                PLAYLIST_KIND_RECENTLY_PLAYED,
                RECENTLY_PLAYED_PLAYLIST_DESCRIPTION,
            )
            == RECENTLY_PLAYED_PLAYLIST_DESCRIPTION
        )


def test_custom_playlist_name_is_never_translated():
    with _language("en"):
        assert (
            localize_system_playlist_name(PLAYLIST_KIND_CUSTOM, "我的片单")
            == "我的片单"
        )
        assert (
            localize_system_playlist_description(PLAYLIST_KIND_CUSTOM, "自建列表")
            == "自建列表"
        )


def test_chinese_catalog_matches_database_names():
    zh = _load_catalog("zh-CN.json")

    assert zh["playlists.system.favorites.name"] == FAVORITES_PLAYLIST_NAME
    assert zh["playlists.system.favorites.description"] == FAVORITES_PLAYLIST_DESCRIPTION
    assert zh["playlists.system.recently_played.name"] == RECENTLY_PLAYED_PLAYLIST_NAME
    assert (
        zh["playlists.system.recently_played.description"]
        == RECENTLY_PLAYED_PLAYLIST_DESCRIPTION
    )


def test_reserved_names_cover_every_language():
    reserved = system_playlist_reserved_names()

    assert FAVORITES_PLAYLIST_NAME in reserved
    assert RECENTLY_PLAYED_PLAYLIST_NAME in reserved
    assert "Favorites" in reserved
    assert "Recently Played" in reserved


def test_playlist_resource_localizes_system_playlist_only(test_db):
    system_playlist = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
        description=FAVORITES_PLAYLIST_DESCRIPTION,
    )
    custom_playlist = Playlist.create(
        kind=PLAYLIST_KIND_CUSTOM,
        name="我的片单",
        description="自建列表",
    )

    with _language("en"):
        resource = PlaylistResource.from_playlist(system_playlist, movie_count=3)
        assert resource.name == "Favorites"
        assert resource.description == "Movies you have favorited manually"
        assert resource.movie_count == 3

        custom_resource = PlaylistResource.from_playlist(custom_playlist)
        assert custom_resource.name == "我的片单"
        assert custom_resource.description == "自建列表"

        summary = PlaylistSummaryResource.from_playlist(system_playlist)
        assert summary.name == "Favorites"
        assert summary.is_system is True

        custom_summary = PlaylistSummaryResource.from_playlist(custom_playlist)
        assert custom_summary.name == "我的片单"
        assert custom_summary.is_system is False
