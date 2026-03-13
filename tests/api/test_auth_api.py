from src.model import UserRefreshToken


def test_create_access_token_returns_token_resource(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["user"] == {"username": account_user.username}
    assert UserRefreshToken.select().count() == 1


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
