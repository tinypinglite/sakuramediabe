from datetime import datetime, timedelta, timezone

import pytest

from src.model import User, UserRefreshToken


@pytest.fixture()
def refresh_token_tables(test_db):
    test_db.bind([User, UserRefreshToken], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([User, UserRefreshToken])
    yield test_db
    test_db.drop_tables([UserRefreshToken, User])


def test_refresh_token_belongs_to_user(refresh_token_tables):
    User.create(username="alice", password_hash="hashed")

    token = UserRefreshToken.create(
        token_id="token-1",
        token_hash="hashed-token",
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    assert "user" not in UserRefreshToken._meta.fields


def test_refresh_token_can_track_rotation(refresh_token_tables):
    User.create(username="bob", password_hash="hashed")

    token = UserRefreshToken.create(
        token_id="token-2",
        token_hash="hashed-token",
        status="revoked",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        replaced_by_token_id="token-3",
    )

    assert token.replaced_by_token_id == "token-3"
