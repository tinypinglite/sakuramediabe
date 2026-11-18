def _token(client, user) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": user.username, "password": "password123"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def _missing_plugin_response(client, token, *, headers=None):
    merged = {"Authorization": f"Bearer {token}"}
    if headers:
        merged.update(headers)
    return client.get("/system/plugins/does-not-exist", headers=merged)


def test_response_adds_accept_language_vary(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert "accept-language" in response.headers["vary"].lower()


def test_english_header_localizes_known_error(client, account_user):
    token = _token(client, account_user)

    response = _missing_plugin_response(
        client, token, headers={"Accept-Language": "en"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Plugin not found"
    assert "accept-language" in response.headers["vary"].lower()


def test_user_language_is_used_without_header(client, account_user):
    account_user.language = "en"
    account_user.save()
    token = _token(client, account_user)

    response = _missing_plugin_response(client, token)

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Plugin not found"


def test_header_overrides_user_language(client, account_user):
    account_user.language = "en"
    account_user.save()
    token = _token(client, account_user)

    response = _missing_plugin_response(
        client, token, headers={"Accept-Language": "zh-CN"}
    )

    assert response.status_code == 404
    assert "未知插件" in response.json()["error"]["message"]


def test_invalid_header_falls_back_to_user_language(client, account_user):
    account_user.language = "en"
    account_user.save()
    token = _token(client, account_user)

    response = _missing_plugin_response(
        client, token, headers={"Accept-Language": "zh-TW"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Plugin not found"


def test_language_state_does_not_leak_between_requests(client, account_user):
    token = _token(client, account_user)

    english = _missing_plugin_response(
        client, token, headers={"Accept-Language": "en"}
    )
    chinese = _missing_plugin_response(
        client, token, headers={"Accept-Language": "zh-CN"}
    )

    assert english.json()["error"]["message"] == "Plugin not found"
    assert "未知插件" in chinese.json()["error"]["message"]
