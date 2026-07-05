from peewee import PostgresqlDatabase

from src.config.config import Database, DatabaseEngine
from src.model import Actor, Movie
from src.model.base import create_database


def test_create_database_returns_postgres_backend():
    db = create_database(
        Database(engine=DatabaseEngine.POSTGRES, url="postgresql://user:pass@localhost:5432/app")
    )

    assert isinstance(db, PostgresqlDatabase)


def test_javdb_id_fields_use_postgres_default_case_sensitive_comparison():
    db = create_database(
        Database(engine=DatabaseEngine.POSTGRES, url="postgresql://user:pass@localhost:5432/app")
    )
    original_actor_db = Actor._meta.database
    original_movie_db = Movie._meta.database

    try:
        Actor._meta.set_database(db)
        Movie._meta.set_database(db)

        actor_column_sql = db.get_sql_context().sql(Actor.javdb_id.ddl(db.get_sql_context())).query()[0]
        movie_column_sql = db.get_sql_context().sql(Movie.javdb_id.ddl(db.get_sql_context())).query()[0]

        assert "COLLATE" not in actor_column_sql
        assert "COLLATE" not in movie_column_sql
    finally:
        Actor._meta.set_database(original_actor_db)
        Movie._meta.set_database(original_movie_db)


def test_create_database_requires_postgres_url():
    try:
        create_database(Database(engine=DatabaseEngine.POSTGRES, url=""))
    except ValueError as exc:
        assert str(exc) == "Database url is required when engine is postgres"
    else:
        raise AssertionError("Expected ValueError when postgres url is missing")
