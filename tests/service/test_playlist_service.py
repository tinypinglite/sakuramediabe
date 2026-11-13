import pytest

from src.api.exception.errors import ApiError
from src.model import (
    FAVORITES_PLAYLIST_NAME,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Movie,
    Playlist,
    PlaylistMovie,
)
from src.schema.collections.playlists import PlaylistCreateRequest
from src.service.catalog.movie_service import MovieService
from src.service.collections.playlist_service import PlaylistService


def _create_movie(number: str = "FAV-001") -> Movie:
    return Movie.create(
        movie_number=number,
        title=number,
    )


def test_favorites_playlist_allows_member_changes(test_db):
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
    )
    movie = _create_movie()

    PlaylistService.add_movie_to_playlist(playlist.id, movie.movie_number)
    assert (
        PlaylistMovie.select()
        .where(PlaylistMovie.playlist == playlist)
        .count()
        == 1
    )

    PlaylistService.remove_movie_from_playlist(playlist.id, movie.movie_number)
    assert (
        PlaylistMovie.select()
        .where(PlaylistMovie.playlist == playlist)
        .count()
        == 0
    )


def test_batch_add_movies_updates_existing_members_and_skips_missing(test_db):
    playlist = Playlist.create(name="批量片单")
    existing = _create_movie("BATCH-EXIST-001")
    added = _create_movie("BATCH-NEW-001")
    PlaylistMovie.create(playlist=playlist, movie=existing)

    result = PlaylistService.add_movies_to_playlist(
        playlist.id,
        [existing.movie_number, "BATCH-MISSING", added.movie_number],
    )

    assert result.requested_count == 3
    assert result.updated_count == 2
    assert result.skipped_count == 1
    assert result.skipped[0].movie_number == "BATCH-MISSING"
    assert result.skipped[0].reason == "movie_not_found"
    assert {
        link.movie.movie_number
        for link in PlaylistMovie.select().where(PlaylistMovie.playlist == playlist)
    } == {"BATCH-EXIST-001", "BATCH-NEW-001"}


def test_recently_played_playlist_rejects_member_changes(test_db):
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
    )
    movie = _create_movie()

    with pytest.raises(ApiError) as error:
        PlaylistService.add_movie_to_playlist(playlist.id, movie.movie_number)

    assert error.value.status_code == 409
    assert error.value.code == "playlist_managed_by_system"


def test_favorites_playlist_cannot_be_deleted(test_db):
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
    )

    with pytest.raises(ApiError) as error:
        PlaylistService.delete_playlist(playlist.id)

    assert error.value.status_code == 409
    assert error.value.code == "playlist_managed_by_system"


def test_favorites_name_is_reserved_for_user_playlists(test_db):
    with pytest.raises(ApiError) as error:
        PlaylistService.create_playlist(
            PlaylistCreateRequest(name=FAVORITES_PLAYLIST_NAME)
        )

    assert error.value.status_code == 409
    assert error.value.code == "playlist_reserved_name"


def test_movie_detail_exposes_favorites_playlist_id(test_db):
    playlist = Playlist.create(
        kind=PLAYLIST_KIND_FAVORITES,
        name=FAVORITES_PLAYLIST_NAME,
    )
    movie = _create_movie()

    resource = MovieService.get_movie_detail(movie.movie_number)

    assert resource.favorite_playlist_id == playlist.id
