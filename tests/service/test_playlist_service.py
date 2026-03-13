from datetime import datetime

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Media,
    MediaProgress,
    Movie,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
)
from src.schema.collections.playlists import PlaylistCreateRequest, PlaylistUpdateRequest
from src.schema.playback.media import MediaProgressUpdateRequest
from src.service.collections import PlaylistService
from src.service.playback import MediaService


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
