import pytest
from peewee import IntegrityError

from src.model import Actor, Image, Movie, MovieSeries


def test_actor_normalizes_javdb_id_name_and_alias_name(test_db):
    test_db.bind([Image, Actor], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([Image, Actor])

    actor = Actor.create(
        javdb_id="  AbC123  ",
        name="  三上悠亚  ",
        alias_name="  三上悠亚 / 鬼头桃菜  ",
    )

    assert actor.javdb_id == "AbC123"
    assert actor.name == "三上悠亚"
    assert actor.alias_name == "三上悠亚 / 鬼头桃菜"


def test_actor_name_and_alias_name_do_not_need_to_be_unique(test_db):
    test_db.bind([Image, Actor], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([Image, Actor])

    Actor.create(javdb_id="ActorA1", name="河北彩花", alias_name="河北彩花 / 河北彩伽")
    duplicate = Actor.create(javdb_id="ActorB2", name="河北彩花", alias_name="河北彩花 / 河北彩伽")

    assert duplicate.name == "河北彩花"
    assert duplicate.alias_name == "河北彩花 / 河北彩伽"


def test_actor_javdb_id_is_unique_and_case_sensitive_in_sqlite(test_db):
    test_db.bind([Image, Actor], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([Image, Actor])

    Actor.create(javdb_id="AbC123", name="三上悠亚")
    Actor.create(javdb_id="aBC123", name="鬼头桃菜")

    with pytest.raises(IntegrityError):
        Actor.create(javdb_id="AbC123", name="河北彩花")


def test_movie_javdb_id_is_unique_and_case_sensitive_in_sqlite(test_db):
    test_db.bind([Image, MovieSeries, Movie], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([Image, MovieSeries, Movie])

    Movie.create(javdb_id="MvAbC123", movie_number="ABC-001", title="Movie 1")
    Movie.create(javdb_id="mVaBC123", movie_number="ABC-002", title="Movie 2")

    with pytest.raises(IntegrityError):
        Movie.create(javdb_id="MvAbC123", movie_number="ABC-003", title="Movie 3")
