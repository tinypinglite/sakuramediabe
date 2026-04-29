import pytest

from src.config.config import settings
from src.model import Image, Movie, MovieSeries
from src.service.catalog.movie_collection_service import MovieCollectionService


@pytest.fixture()
def movie_collection_tables(test_db):
    models = [Image, MovieSeries, Movie]
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
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)

    assert MovieCollectionService.matches_configured_collection("OFJE-456", 120) is True
    assert MovieCollectionService.matches_configured_collection("FC2-PPV-123456", 120) is True
    assert MovieCollectionService.matches_configured_collection("ABP-123", 120) is False


def test_matches_configured_collection_supports_duration_threshold(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "others_number_features", set())
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)

    assert MovieCollectionService.matches_configured_collection("ABP-123", 301) is True
    assert MovieCollectionService.matches_configured_collection("ABP-123", 300) is False
    assert MovieCollectionService.matches_configured_collection("ABP-123", 0) is False


def test_sync_movie_collections_updates_matching_and_non_matching_movies(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE", "DVAJ"})
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie("OFJE-001", "MovieA1", is_collection=False, duration_minutes=120)
    _create_movie("DVAJ-002", "MovieA2", is_collection=False, duration_minutes=120)
    _create_movie("ABP-003", "MovieA3", is_collection=True, duration_minutes=120)
    _create_movie("ABP-004", "MovieA4", is_collection=False, duration_minutes=120)

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
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie("OFJE-001", "MovieA1", is_collection=True, duration_minutes=120)
    _create_movie("ABP-002", "MovieA2", is_collection=False, duration_minutes=120)

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
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie("OFJE-001", "MovieA1", is_collection=True, duration_minutes=120)
    _create_movie("ABP-002", "MovieA2", is_collection=False, duration_minutes=120)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 2,
        "matched_count": 0,
        "updated_to_collection_count": 0,
        "updated_to_single_count": 1,
        "unchanged_count": 1,
    }
    assert Movie.get(Movie.movie_number == "OFJE-001").is_collection is False


def test_sync_movie_collections_skips_movies_marked_as_manual_override(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie(
        "OFJE-001",
        "MovieA1",
        is_collection=False,
        is_collection_overridden=True,
        duration_minutes=120,
    )
    _create_movie(
        "ABP-002",
        "MovieA2",
        is_collection=True,
        is_collection_overridden=True,
        duration_minutes=360,
    )
    _create_movie(
        "OFJE-003",
        "MovieA3",
        is_collection=False,
        is_collection_overridden=False,
        duration_minutes=120,
    )

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 3,
        "matched_count": 3,
        "updated_to_collection_count": 1,
        "updated_to_single_count": 0,
        "unchanged_count": 2,
    }
    assert Movie.get(Movie.movie_number == "OFJE-001").is_collection is False
    assert Movie.get(Movie.movie_number == "ABP-002").is_collection is True
    assert Movie.get(Movie.movie_number == "OFJE-003").is_collection is True


def test_sync_movie_collections_marks_movie_as_collection_when_duration_exceeds_threshold(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", set())
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie("ABP-001", "MovieA1", is_collection=False, duration_minutes=301)
    _create_movie("ABP-002", "MovieA2", is_collection=False, duration_minutes=300)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 2,
        "matched_count": 1,
        "updated_to_collection_count": 1,
        "updated_to_single_count": 0,
        "unchanged_count": 1,
    }
    assert Movie.get(Movie.movie_number == "ABP-001").is_collection is True
    assert Movie.get(Movie.movie_number == "ABP-002").is_collection is False


def test_sync_movie_collections_supports_prefix_or_duration_rule(
    movie_collection_tables,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    _create_movie("OFJE-001", "MovieA1", is_collection=False, duration_minutes=120)
    _create_movie("ABP-002", "MovieA2", is_collection=False, duration_minutes=360)
    _create_movie("ABP-003", "MovieA3", is_collection=False, duration_minutes=120)

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 3,
        "matched_count": 2,
        "updated_to_collection_count": 2,
        "updated_to_single_count": 0,
        "unchanged_count": 1,
    }
    assert Movie.get(Movie.movie_number == "OFJE-001").is_collection is True
    assert Movie.get(Movie.movie_number == "ABP-002").is_collection is True
    assert Movie.get(Movie.movie_number == "ABP-003").is_collection is False
