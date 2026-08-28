"""索引器配置接口回归：Torznab 化后的配置语义。

review 发现的老前端兼容风险：
- 旧前端整表保存时不带 api_key，若按默认 None 落库会把已配 key 全部静默清空；
  因此「省略 api_key」必须沿用同名现有索引器的 key，只有显式传 null/空串才清空。
"""

from src.model import DownloadClient, Indexer, MediaLibrary


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_download_client() -> DownloadClient:
    library = MediaLibrary.create(
        name="local-lib",
        provider_key="test",
        provider_config={},
    )
    return DownloadClient.create(
        name="client-main",
        library=library,
        provider_config={},
    )


def _seed_indexer(*, name: str = "mteam", api_key: str | None = "keep-me") -> Indexer:
    return Indexer.create(
        name=name,
        url="http://torznab/api",
        kind="pt",
        api_key=api_key,
    )


def test_patch_persists_explicit_api_key(client, account_user):
    token = _login(client, account_user.username)
    download_client = _create_download_client()

    response = client.patch(
        "/indexer-settings",
        headers=_auth(token),
        json={
            "indexers": [
                {
                    "name": "mteam",
                    "url": "http://torznab/api",
                    "kind": "pt",
                    "api_key": "secret-key",
                    "download_client_ids": [download_client.id],
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["indexers"][0]["api_key"] == "secret-key"
    assert Indexer.get(Indexer.name == "mteam").api_key == "secret-key"


def test_patch_omitting_api_key_preserves_existing_key(client, account_user):
    """旧前端整表保存：省略 api_key 不得清空已配置的 key。"""
    token = _login(client, account_user.username)
    download_client = _create_download_client()
    _seed_indexer(api_key="keep-me")

    response = client.patch(
        "/indexer-settings",
        headers=_auth(token),
        json={
            "indexers": [
                {
                    "name": "mteam",
                    "url": "http://torznab/api",
                    "kind": "pt",
                    "download_client_ids": [download_client.id],
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["indexers"][0]["api_key"] == "keep-me"
    assert Indexer.get(Indexer.name == "mteam").api_key == "keep-me"


def test_patch_explicit_null_clears_api_key(client, account_user):
    token = _login(client, account_user.username)
    download_client = _create_download_client()
    _seed_indexer(api_key="old-key")

    response = client.patch(
        "/indexer-settings",
        headers=_auth(token),
        json={
            "indexers": [
                {
                    "name": "mteam",
                    "url": "http://torznab/api",
                    "kind": "pt",
                    "api_key": None,
                    "download_client_ids": [download_client.id],
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["indexers"][0]["api_key"] is None
    assert Indexer.get(Indexer.name == "mteam").api_key is None


def test_patch_empty_body_still_rejected(client, account_user):
    token = _login(client, account_user.username)

    response = client.patch("/indexer-settings", headers=_auth(token), json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_indexer_settings_update"
