from src.model import User, UserRefreshToken


def test_create_access_token_returns_token_resource(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["user"] == {
        "username": account_user.username,
        "language": None,
    }
    assert "Z" not in body["expires_at"]
    assert "Z" not in body["refresh_expires_at"]
    assert UserRefreshToken.select().count() == 1


def test_first_login_persists_accept_language(client, account_user):
    response = client.post(
        "/auth/tokens",
        headers={"Accept-Language": "en-US"},
        json={"username": account_user.username, "password": "password123"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["language"] == "en"
    assert User.get_by_id(account_user.id).language == "en"


def test_existing_language_is_not_overwritten_by_login_header(client, account_user):
    account_user.language = "zh-CN"
    account_user.save()

    response = client.post(
        "/auth/tokens",
        headers={"Accept-Language": "en"},
        json={"username": account_user.username, "password": "password123"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["language"] == "zh-CN"
    assert User.get_by_id(account_user.id).language == "zh-CN"


def test_invalid_credentials_message_is_localized_in_english(client, account_user):
    response = client.post(
        "/auth/tokens",
        headers={"Accept-Language": "en"},
        json={"username": account_user.username, "password": "wrong-password"},
    )

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "invalid_credentials"
    assert body["error"]["message"] == "Username or password is incorrect"


def test_invalid_credentials_message_is_unchanged_in_chinese(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Username or password is incorrect"


def test_create_access_token_rejects_invalid_credentials(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_refresh_token_rotates_and_revokes_previous_token(client, account_user):
    create_response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )

    refresh_token = create_response.json()["refresh_token"]
    access_token = create_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}
    refresh_response = client.post(
        "/auth/token-refreshes",
        json={"refresh_token": refresh_token},
        headers=headers,
    )
    second_refresh_response = client.post(
        "/auth/token-refreshes",
        json={"refresh_token": refresh_token},
        headers=headers,
    )

    assert refresh_response.status_code == 201
    assert refresh_response.json()["refresh_token"] != refresh_token
    assert second_refresh_response.status_code == 401
    assert second_refresh_response.json()["error"]["code"] == "invalid_refresh_token"


def test_refresh_token_requires_access_token(client, account_user):
    create_response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    refresh_token = create_response.json()["refresh_token"]

    refresh_response = client.post(
        "/auth/token-refreshes",
        json={"refresh_token": refresh_token},
    )

    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "unauthorized"


def test_docs_oauth_token_endpoint_accepts_username_password_form(client, account_user):
    response = client.post(
        "/auth/docs-token",
        data={"username": account_user.username, "password": "password123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
