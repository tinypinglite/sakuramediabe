import pytest

from src.model import Image, Movie, MovieSeries
from src.service.catalog.movie_thin_cover_backfill_service import MovieThinCoverBackfillService


class _FakeImportService:
    def __init__(self, outcomes_by_movie_number: dict[str, bool], failures: set[str] | None = None):
        self.outcomes_by_movie_number = outcomes_by_movie_number
        self.failures = failures or set()

    def backfill_movie_thin_cover(self, movie: Movie) -> bool:
        if movie.movie_number in self.failures:
            raise RuntimeError(f"boom:{movie.movie_number}")
        return self.outcomes_by_movie_number.get(movie.movie_number, False)


@pytest.fixture()
def movie_tables(test_db):
    models = [Image, MovieSeries, Movie]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def test_backfill_missing_thin_cover_images_updates_and_skips(movie_tables):
    Movie.create(javdb_id="javdb-ABP-301", movie_number="ABP-301", title="A")
    Movie.create(javdb_id="javdb-ABP-302", movie_number="ABP-302", title="B")

    service = MovieThinCoverBackfillService(
        import_service=_FakeImportService(
            outcomes_by_movie_number={
                "ABP-301": True,
                "ABP-302": False,
            }
        )
    )

    stats = service.backfill_missing_thin_cover_images()

    assert stats == {
        "scanned_movies": 2,
        "updated_movies": 1,
        "skipped_movies": 1,
        "failed_movies": 0,
    }


def test_backfill_missing_thin_cover_images_counts_failures_and_continues(movie_tables):
    Movie.create(javdb_id="javdb-ABP-303", movie_number="ABP-303", title="A")
    Movie.create(javdb_id="javdb-ABP-304", movie_number="ABP-304", title="B")

    service = MovieThinCoverBackfillService(
        import_service=_FakeImportService(
            outcomes_by_movie_number={"ABP-304": True},
            failures={"ABP-303"},
        )
    )

    stats = service.backfill_missing_thin_cover_images()

    assert stats == {
        "scanned_movies": 2,
        "updated_movies": 1,
        "skipped_movies": 0,
        "failed_movies": 1,
    }
