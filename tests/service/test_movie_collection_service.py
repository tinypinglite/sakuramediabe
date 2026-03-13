import pytest

from src.config.config import settings
from src.model import Image, Movie
from src.service.catalog.movie_collection_service import MovieCollectionService


@pytest.fixture()
def movie_collection_tables(test_db):
    models = [Image, Movie]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_matches_configured_collection_supports_normalized_prefixes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "others_number_features", {"ofje", "fc2"})

    assert MovieCollectionService.matches_configured_collection("OFJE-456") is True
    assert MovieCollectionService.matches_configured_collection("FC2-PPV-123456") is True
    assert MovieCollectionService.matches_configured_collection("ABP-123") is False


def test_sync_movie_collections_updates_matching_and_non_matching_movies(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE", "DVAJ"})
    _create_movie("OFJE-001", "MovieA1", is_collection=False)
    _create_movie("DVAJ-002", "MovieA2", is_collection=False)
    _create_movie("ABP-003", "MovieA3", is_collection=True)
    _create_movie("ABP-004", "MovieA4", is_collection=False)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 4,
        "matched_count": 2,
        "updated_to_collection_count": 2,
        "updated_to_single_count": 1,
        "unchanged_count": 1,
    }
    assert Movie.get(Movie.movie_number == "OFJE-001").is_collection is True
    assert Movie.get(Movie.movie_number == "DVAJ-002").is_collection is True
    assert Movie.get(Movie.movie_number == "ABP-003").is_collection is False
    assert Movie.get(Movie.movie_number == "ABP-004").is_collection is False


def test_sync_movie_collections_keeps_existing_target_state(movie_collection_tables, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    _create_movie("OFJE-001", "MovieA1", is_collection=True)
    _create_movie("ABP-002", "MovieA2", is_collection=False)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 2,
        "matched_count": 1,
        "updated_to_collection_count": 0,
        "updated_to_single_count": 0,
        "unchanged_count": 2,
    }


def test_sync_movie_collections_marks_all_movies_as_single_when_prefixes_empty(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", set())
    _create_movie("OFJE-001", "MovieA1", is_collection=True)
    _create_movie("ABP-002", "MovieA2", is_collection=False)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 2,
        "matched_count": 0,
        "updated_to_collection_count": 0,
        "updated_to_single_count": 1,
        "unchanged_count": 1,
    }
    assert Movie.get(Movie.movie_number == "OFJE-001").is_collection is False
