from src.config.config import Database, DatabaseEngine
from src.model import Movie
from src.model.base import create_database
from src.service.catalog.movie_heat_service import MovieHeatService


def test_update_movie_heat_returns_zero_for_empty_table(app):
    stats = MovieHeatService.update_movie_heat()

    assert stats == {
        "candidate_count": 0,
        "updated_count": 0,
        "formula_version": "v1",
    }


def test_update_movie_heat_only_updates_movies_with_changed_heat(app):
    unchanged = Movie.create(
        javdb_id="javdb-heat-1",
        movie_number="HEAT-001",
        title="HEAT-001",
        want_watch_count=1,
        comment_count=1,
        score_number=1,
        heat=1,
    )
    changed = Movie.create(
        javdb_id="javdb-heat-2",
        movie_number="HEAT-002",
        title="HEAT-002",
        want_watch_count=10,
        comment_count=2,
        score_number=3,
        heat=0,
    )
    also_changed = Movie.create(
        javdb_id="javdb-heat-3",
        movie_number="HEAT-003",
        title="HEAT-003",
        want_watch_count=0,
        comment_count=0,
        score_number=2,
        heat=99,
    )

    stats = MovieHeatService.update_movie_heat()

    unchanged = Movie.get_by_id(unchanged.id)
    changed = Movie.get_by_id(changed.id)
    also_changed = Movie.get_by_id(also_changed.id)

    assert stats == {
        "candidate_count": 2,
        "updated_count": 2,
        "formula_version": "v1",
    }
    assert unchanged.heat == 1
    assert changed.heat == 5
    assert also_changed.heat == 0


def test_update_movie_heat_skips_second_write_when_heat_is_already_up_to_date(app):
    movie = Movie.create(
        javdb_id="javdb-heat-4",
        movie_number="HEAT-004",
        title="HEAT-004",
        want_watch_count=4,
        comment_count=1,
        score_number=2,
        heat=0,
    )

    first_run = MovieHeatService.update_movie_heat()
    second_run = MovieHeatService.update_movie_heat()
    movie = Movie.get_by_id(movie.id)

    assert first_run == {
        "candidate_count": 1,
        "updated_count": 1,
        "formula_version": "v1",
    }
    assert second_run == {
        "candidate_count": 0,
        "updated_count": 0,
        "formula_version": "v1",
    }
    assert movie.heat == 2


def test_build_update_query_compiles_for_supported_backends():
    backends = [
        Database(engine=DatabaseEngine.SQLITE, path=":memory:"),
        Database(engine=DatabaseEngine.MYSQL, url="mysql://user:pass@localhost:3306/app"),
        Database(engine=DatabaseEngine.POSTGRES, url="postgresql://user:pass@localhost:5432/app"),
    ]
    original_db = Movie._meta.database

    try:
        for backend in backends:
            database = create_database(backend)
            Movie._meta.set_database(database)

            sql, params = MovieHeatService.build_update_query().sql()
            normalized_sql = sql.lower()

            assert "update" in normalized_sql
            assert "movie" in normalized_sql
            assert "set" in normalized_sql
            assert "heat" in normalized_sql
            assert "where" in normalized_sql
            assert "round" in normalized_sql
            assert "cast(" in normalized_sql
            assert " from " not in normalized_sql
            assert " returning " not in normalized_sql
            assert "::" not in normalized_sql
            assert params
    finally:
        Movie._meta.set_database(original_db)
