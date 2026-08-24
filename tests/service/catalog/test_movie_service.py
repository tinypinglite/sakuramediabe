from src.model import Movie
from src.schema.catalog.movies import MovieCollectionMarkType
from src.service.catalog.movie_service import MovieService


def test_manual_collection_mark_writes_host_owner(test_db):
    movie = Movie.create(
        javdb_id="javdb-ABP-001",
        movie_number="ABP-001",
        title="ABP-001",
        is_collection=True,
    )

    response = MovieService.mark_movie_collection_type(
        ["abp-001"], MovieCollectionMarkType.SINGLE
    )

    assert response.updated_count == 1
    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is False
    assert movie.field_owners == {"is_collection": "host:manual"}
