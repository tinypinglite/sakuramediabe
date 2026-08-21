from src.config.config import settings
from src.model import Movie
from src.schema.catalog.movies import MovieCollectionMarkType
from src.service.catalog.movie_collection_service import MovieCollectionService
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway
from src.service.catalog.movie_service import MovieService


def _create_movie(test_db, movie_number: str, *, is_collection: bool = False) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=movie_number,
        is_collection=is_collection,
    )


def test_collection_sync_only_updates_unowned_fields(test_db, monkeypatch):
    monkeypatch.setattr(settings.media, "others_number_features", {"COLLECTION"})
    automatic = _create_movie(test_db, "COLLECTION-001")
    manual = _create_movie(test_db, "COLLECTION-002")
    plugin_owned = _create_movie(test_db, "COLLECTION-003")
    ordinary = _create_movie(test_db, "ABP-001", is_collection=True)

    MovieOwnershipGateway.update_host_manual([manual.id], {"is_collection": False})
    MovieOwnershipGateway.patch_plugin(
        plugin_owned.id,
        "collection-plugin",
        {"is_collection": False},
        expected_revision=0,
    )

    stats = MovieCollectionService.sync_movie_collections()

    assert stats == {
        "total_movies": 4,
        "matched_count": 3,
        "updated_to_collection_count": 1,
        "updated_to_single_count": 1,
        "unchanged_count": 2,
    }
    assert Movie.get_by_id(automatic.id).is_collection is True
    assert Movie.get_by_id(manual.id).is_collection is False
    assert Movie.get_by_id(plugin_owned.id).is_collection is False
    assert Movie.get_by_id(ordinary.id).is_collection is False


def test_manual_collection_mark_writes_host_owner(test_db):
    movie = _create_movie(test_db, "ABP-001", is_collection=True)

    response = MovieService.mark_movie_collection_type(
        ["abp-001"], MovieCollectionMarkType.SINGLE
    )

    assert response.updated_count == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is False
    assert movie.field_owners == {"is_collection": "host:manual"}
