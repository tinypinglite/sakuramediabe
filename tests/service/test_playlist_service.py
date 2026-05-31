from datetime import datetime

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Media,
    MediaProgress,
    Movie,
    PLAYLIST_KIND_4K,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    PLAYLIST_KIND_VR,
    Playlist,
    PlaylistMovie,
)
from src.schema.collections.playlists import PlaylistCreateRequest, PlaylistUpdateRequest
from src.schema.playback.media import MediaProgressUpdateRequest
from src.service.collections import PlaylistService
from src.service.playback import MediaService
from src.start.initdb import init_system_playlists


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_create_playlist_rejects_reserved_and_duplicate_names(app):
    Playlist.create(name="我的收藏", description="Favorite")

    with pytest.raises(ApiError) as reserved_exc:
        PlaylistService.create_playlist(PlaylistCreateRequest(name="最近播放", description=""))

    with pytest.raises(ApiError) as duplicate_exc:
        PlaylistService.create_playlist(PlaylistCreateRequest(name="我的收藏", description=""))

    assert reserved_exc.value.code == "playlist_reserved_name"
    assert duplicate_exc.value.code == "playlist_name_conflict"


def test_add_movie_to_playlist_is_idempotent_and_refreshes_timestamps(app, monkeypatch):
    playlist = Playlist.create(name="我的收藏", description="Favorite")
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    first_time = datetime(2026, 3, 12, 10, 0, 0)
    second_time = datetime(2026, 3, 12, 10, 5, 0)

    monkeypatch.setattr(PlaylistService, "_current_time", lambda: first_time)
    PlaylistService.add_movie_to_playlist(playlist.id, movie.movie_number)

    monkeypatch.setattr(PlaylistService, "_current_time", lambda: second_time)
    PlaylistService.add_movie_to_playlist(playlist.id, movie.movie_number)

    playlist_movie = PlaylistMovie.get(PlaylistMovie.playlist == playlist, PlaylistMovie.movie == movie)
    playlist = Playlist.get_by_id(playlist.id)

    assert PlaylistMovie.select().count() == 1
    assert playlist_movie.updated_at == second_time
    assert playlist.updated_at == second_time


def test_system_playlist_cannot_be_mutated_manually(app):
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")

    with pytest.raises(ApiError) as update_exc:
        PlaylistService.update_playlist(
            playlist.id,
            PlaylistUpdateRequest(name="新名字"),
        )
    with pytest.raises(ApiError) as add_exc:
        PlaylistService.add_movie_to_playlist(playlist.id, movie.movie_number)
    with pytest.raises(ApiError) as delete_exc:
        PlaylistService.delete_playlist(playlist.id)

    assert update_exc.value.code == "playlist_managed_by_system"
    assert add_exc.value.code == "playlist_managed_by_system"
    assert delete_exc.value.code == "playlist_managed_by_system"


def test_list_playlist_movies_orders_by_relation_updated_at_desc(app):
    playlist = Playlist.create(name="我的收藏", description="Favorite")
    first_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    first_link = PlaylistMovie.create(playlist=playlist, movie=first_movie)
    second_link = PlaylistMovie.create(playlist=playlist, movie=second_movie)

    PlaylistMovie.update(updated_at="2026-03-12 10:00:00").where(PlaylistMovie.id == first_link.id).execute()
    PlaylistMovie.update(updated_at="2026-03-12 11:00:00").where(PlaylistMovie.id == second_link.id).execute()

    response = PlaylistService.list_playlist_movies(playlist.id, page=1, page_size=20)

    assert response.model_dump()["items"][0]["movie_number"] == "ABC-002"
    assert response.model_dump()["items"][1]["movie_number"] == "ABC-001"


def test_update_progress_creates_media_progress_and_recently_played_membership(app, monkeypatch):
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    watched_at = datetime(2026, 3, 12, 12, 0, 0)

    monkeypatch.setattr(MediaService, "_current_time", lambda: watched_at)
    monkeypatch.setattr(PlaylistService, "_current_time", lambda: watched_at)

    response = MediaService.update_progress(
        media.id,
        MediaProgressUpdateRequest(position_seconds=600),
    )

    progress = MediaProgress.get(MediaProgress.media == media)
    playlist = Playlist.get(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)
    playlist_movie = PlaylistMovie.get(PlaylistMovie.playlist == playlist, PlaylistMovie.movie == movie)

    assert response.model_dump(mode="json") == {
        "media_id": media.id,
        "last_position_seconds": 600,
        "last_watched_at": "2026-03-12T12:00:00",
    }
    assert progress.position_seconds == 600
    assert progress.last_watched_at == watched_at
    assert playlist_movie.updated_at == watched_at


def test_create_playlist_rejects_vr_and_4k_reserved_names(app):
    for reserved_name in ("VR", "4K"):
        with pytest.raises(ApiError) as exc:
            PlaylistService.create_playlist(PlaylistCreateRequest(name=reserved_name, description=""))
        assert exc.value.code == "playlist_reserved_name"


def test_list_playlists_includes_virtual_system_playlists_with_dynamic_counts(app):
    init_system_playlists()
    # 两部 4K、一部 VR、一部普通，验证虚拟列表 movie_count 实时派生。
    vr_movie = _create_movie("VR-001", "MovieVR1", title="VR 1")
    four_k_movie_a = _create_movie("ABC-101", "Movie4K1", title="4K 1")
    four_k_movie_b = _create_movie("ABC-102", "Movie4K2", title="4K 2")
    plain_movie = _create_movie("ABC-200", "MoviePlain", title="Plain")
    Media.create(movie=vr_movie, path="/library/vr-001.mp4", valid=True, special_tags="VR")
    Media.create(movie=four_k_movie_a, path="/library/abc-101.mp4", valid=True, special_tags="4K 中字")
    Media.create(movie=four_k_movie_b, path="/library/abc-102.mp4", valid=True, special_tags="4K")
    Media.create(movie=plain_movie, path="/library/abc-200.mp4", valid=True, special_tags="普通")

    playlists = {playlist.kind: playlist for playlist in PlaylistService.list_playlists()}

    assert playlists[PLAYLIST_KIND_VR].movie_count == 1
    assert playlists[PLAYLIST_KIND_4K].movie_count == 2
    # 虚拟系统列表受保护、不可变。
    assert playlists[PLAYLIST_KIND_VR].is_system is True
    assert playlists[PLAYLIST_KIND_VR].is_mutable is False
    assert playlists[PLAYLIST_KIND_VR].is_deletable is False


def test_list_playlists_orders_system_playlists_before_custom(app):
    init_system_playlists()
    Playlist.create(name="我的收藏", description="Favorite")

    kinds = [playlist.kind for playlist in PlaylistService.list_playlists()]

    # 系统列表固定排在自定义列表之前，且内部按 最近播放 -> VR -> 4K 稳定排序。
    assert kinds[:3] == [PLAYLIST_KIND_RECENTLY_PLAYED, PLAYLIST_KIND_VR, PLAYLIST_KIND_4K]
    assert kinds[3] == "custom"


def test_virtual_system_playlists_cannot_be_mutated_manually(app):
    init_system_playlists()
    movie = _create_movie("ABC-101", "Movie4K1", title="4K 1")
    Media.create(movie=movie, path="/library/abc-101.mp4", valid=True, special_tags="4K")
    four_k_playlist = Playlist.get(Playlist.kind == PLAYLIST_KIND_4K)

    with pytest.raises(ApiError) as update_exc:
        PlaylistService.update_playlist(four_k_playlist.id, PlaylistUpdateRequest(name="新名字"))
    with pytest.raises(ApiError) as add_exc:
        PlaylistService.add_movie_to_playlist(four_k_playlist.id, movie.movie_number)
    with pytest.raises(ApiError) as remove_exc:
        PlaylistService.remove_movie_from_playlist(four_k_playlist.id, movie.movie_number)
    with pytest.raises(ApiError) as delete_exc:
        PlaylistService.delete_playlist(four_k_playlist.id)

    assert update_exc.value.code == "playlist_managed_by_system"
    assert add_exc.value.code == "playlist_managed_by_system"
    assert remove_exc.value.code == "playlist_managed_by_system"
    assert delete_exc.value.code == "playlist_managed_by_system"


def test_list_virtual_playlist_movies_filters_by_tag_and_orders_by_media_created_at(app):
    init_system_playlists()
    older_movie = _create_movie("ABC-101", "Movie4K1", title="4K 1")
    newer_movie = _create_movie("ABC-102", "Movie4K2", title="4K 2")
    plain_movie = _create_movie("ABC-200", "MoviePlain", title="Plain")
    # 精确匹配空格分隔标签：含 "4K2" 不应命中 "4K"。
    Media.create(
        movie=older_movie,
        path="/library/abc-101.mp4",
        valid=True,
        special_tags="4K",
        created_at=datetime(2026, 3, 12, 10, 0, 0),
    )
    Media.create(
        movie=newer_movie,
        path="/library/abc-102.mp4",
        valid=True,
        special_tags="4K",
        created_at=datetime(2026, 3, 12, 11, 0, 0),
    )
    Media.create(movie=plain_movie, path="/library/abc-200.mp4", valid=True, special_tags="4K2")

    four_k_playlist = Playlist.get(Playlist.kind == PLAYLIST_KIND_4K)
    response = PlaylistService.list_playlist_movies(four_k_playlist.id, page=1, page_size=20)
    payload = response.model_dump(mode="json")

    assert payload["total"] == 2
    # 按最近媒体入库时间倒序：newer 在前。
    assert [item["movie_number"] for item in payload["items"]] == ["ABC-102", "ABC-101"]
    assert payload["items"][0]["is_4k"] is True
    assert payload["items"][0]["playlist_item_updated_at"] == "2026-03-12T11:00:00"


def test_update_progress_refreshes_existing_recently_played_relation(app, monkeypatch):
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    PlaylistMovie.create(playlist=playlist, movie=movie)

    watched_at = datetime(2026, 3, 12, 13, 0, 0)
    monkeypatch.setattr(MediaService, "_current_time", lambda: watched_at)
    monkeypatch.setattr(PlaylistService, "_current_time", lambda: watched_at)

    MediaService.update_progress(
        media.id,
        MediaProgressUpdateRequest(position_seconds=900),
    )

    playlist_movie = PlaylistMovie.get(PlaylistMovie.playlist == playlist, PlaylistMovie.movie == movie)
    assert PlaylistMovie.select().count() == 1
    assert playlist_movie.updated_at == watched_at
