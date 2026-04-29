import pytest
from peewee import IntegrityError

from src.model import User


@pytest.fixture()
def user_tables(test_db):
    test_db.bind([User], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([User])
    yield test_db
    test_db.drop_tables([User])


def test_user_defaults_role_and_status(user_tables):
    user = User.create(username="account", password_hash="hashed")

    assert user.username == "account"
    assert "role" not in User._meta.fields
    assert "status" not in User._meta.fields
    assert "deleted_at" not in User._meta.fields


def test_username_must_be_unique(user_tables):
    User.create(username="alice", password_hash="hashed")

    with pytest.raises(IntegrityError):
        User.create(username="alice", password_hash="hashed-2")
