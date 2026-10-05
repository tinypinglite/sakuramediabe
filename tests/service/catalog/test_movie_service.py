from src.model import Movie, MovieSeries
from src.schema.catalog.movies import MovieCollectionMarkType
from src.service.catalog.movie_service import MovieService


def test_list_movies_by_series_only_returns_series_movies(test_db):
    series = MovieSeries.create(name="Only series")
    Movie.create(
        javdb_id="javdb-SER-001",
        movie_number="SER-001",
        title="SER-001",
        series=series,
        is_collection=False,
    )
    Movie.create(
        javdb_id="javdb-SER-002",
        movie_number="SER-002",
        title="SER-002",
        is_collection=False,
    )

    response = MovieService.list_movies_by_series(series_id=series.id)

    assert response.total == 1
    assert [item.movie_number for item in response.items] == ["SER-001"]


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
