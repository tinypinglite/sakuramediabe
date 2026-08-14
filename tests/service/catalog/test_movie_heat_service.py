from src.model import Movie
from src.service.catalog.movie_heat_service import MovieHeatService


def _create_movie(movie_number: str, **counts) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=movie_number,
        **counts,
    )


def test_movie_heat_v6_uses_fixed_linear_references(test_db):
    _create_movie("ZERO-001")
    _create_movie(
        "WATCHED-001",
        watched_count=MovieHeatService.WATCHED_COUNT_REFERENCE,
    )
    _create_movie(
        "WATCHED-DOUBLE-001",
        watched_count=MovieHeatService.WATCHED_COUNT_REFERENCE * 2,
    )
    _create_movie(
        "COMMENT-001",
        comment_count=MovieHeatService.COMMENT_COUNT_REFERENCE,
    )
    _create_movie(
        "ALL-001",
        watched_count=MovieHeatService.WATCHED_COUNT_REFERENCE,
        want_watch_count=MovieHeatService.WANT_WATCH_COUNT_REFERENCE,
        comment_count=MovieHeatService.COMMENT_COUNT_REFERENCE,
        score_number=MovieHeatService.SCORE_NUMBER_REFERENCE,
    )

    result = MovieHeatService.update_movie_heat()

    assert result["formula_version"] == "v6"
    assert Movie.get(Movie.movie_number == "ZERO-001").heat == 0
    assert Movie.get(Movie.movie_number == "WATCHED-001").heat == 638
    assert Movie.get(Movie.movie_number == "WATCHED-DOUBLE-001").heat == 1276
    assert Movie.get(Movie.movie_number == "COMMENT-001").heat == 1550
    assert Movie.get(Movie.movie_number == "ALL-001").heat == 3100
