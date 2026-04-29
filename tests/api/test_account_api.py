def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def test_get_account_returns_current_account(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get("/account", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == account_user.username
    assert "created_at" in body
    assert "last_login_at" in body
    assert "Z" not in body["created_at"]
    assert "Z" not in body["last_login_at"]


def test_patch_account_updates_username(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/account",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "renamed-account"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "renamed-account"


def test_change_password_replaces_old_password(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/account/password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "password123",
            "new_password": "new-password123",
        },
    )
    old_login = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    new_login = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "new-password123"},
    )

    assert response.status_code == 204
    assert old_login.status_code == 401
    assert new_login.status_code == 201
