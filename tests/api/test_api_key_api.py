from src.model import ApiKey


def _login(client, username: str, password: str) -> str:
    response = client.post(
        "/auth/tokens", json={"username": username, "password": password}
    )
    return response.json()["access_token"]


def _headers(client, account_user) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_login(client, account_user.username, 'password123')}"
    }


def test_create_list_and_delete_api_key(client, account_user):
    headers = _headers(client, account_user)

    created = client.post(
        "/account/api-keys", json={"name": "MCP server"}, headers=headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "MCP server"
    assert body["key"].startswith("sk-")
    assert body["key_hint"] == body["key"][:11]
    assert body["last_used_at"] is None
    key_id = body["id"]

    listed = client.get("/account/api-keys", headers=headers)
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["id"] == key_id
    # 列表不回传明文 key；库里只存哈希。
    assert "key" not in items[0]
    stored = ApiKey.get_by_id(key_id)
    assert stored.key_hash != body["key"]
    assert len(stored.key_hash) == 64

    deleted = client.delete(f"/account/api-keys/{key_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/account/api-keys", headers=headers).json() == []

    missing = client.delete(f"/account/api-keys/{key_id}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "api_key_not_found"


def test_api_key_authenticates_protected_endpoints(client, account_user):
    headers = _headers(client, account_user)
    created = client.post(
        "/account/api-keys", json={"name": "集成"}, headers=headers
    ).json()
    api_headers = {"Authorization": f"Bearer {created['key']}"}

    account = client.get("/account", headers=api_headers)
    assert account.status_code == 200
    assert account.json()["username"] == account_user.username

    status_response = client.get("/status", headers=api_headers)
    assert status_response.status_code == 200

    # 首次使用会写入 last_used_at。
    stored = ApiKey.get_by_id(created["id"])
    assert stored.last_used_at is not None


def test_deleted_api_key_is_rejected(client, account_user):
    headers = _headers(client, account_user)
    created = client.post("/account/api-keys", json={}, headers=headers).json()
    api_headers = {"Authorization": f"Bearer {created['key']}"}
    assert client.get("/account", headers=api_headers).status_code == 200

    client.delete(f"/account/api-keys/{created['id']}", headers=headers)

    rejected = client.get("/account", headers=api_headers)
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"


def test_invalid_api_key_is_rejected(client, account_user):
    response = client.get(
        "/account", headers={"Authorization": "Bearer sk-not-a-real-key"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_last_used_at_is_throttled(client, account_user):
    headers = _headers(client, account_user)
    created = client.post("/account/api-keys", json={}, headers=headers).json()
    api_headers = {"Authorization": f"Bearer {created['key']}"}

    client.get("/account", headers=api_headers)
    first = ApiKey.get_by_id(created["id"]).last_used_at
    client.get("/account", headers=api_headers)
    second = ApiKey.get_by_id(created["id"]).last_used_at

    # 节流窗口内重复使用不重复写库。
    assert first == second


def test_api_key_management_requires_login(client, account_user):
    assert client.get("/account/api-keys").status_code == 401
    assert client.post("/account/api-keys", json={}).status_code == 401
    assert client.delete("/account/api-keys/1").status_code == 401
