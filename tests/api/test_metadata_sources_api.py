from src.config.config import settings
from src.plugins import PluginMetadataSource
from src.service.catalog.metadata_source_service import MetadataSourceService


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_metadata_sources_expose_actor_movie_capability(
    client, account_user, monkeypatch
):
    actor_movie_source = PluginMetadataSource(
        fetch_movie=lambda _number: None,
        fetch_actor_movies=lambda _query: None,
    )
    plain_source = PluginMetadataSource(fetch_movie=lambda _number: None)
    monkeypatch.setattr(
        MetadataSourceService,
        "sources",
        (
            ("actor_source", "Actor Source", {}, actor_movie_source),
            ("plain_source", "Plain Source", {}, plain_source),
        ),
    )
    monkeypatch.setattr(
        settings.plugins,
        "enabled",
        ["actor_source", "plain_source"],
    )

    response = client.get(
        "/system/metadata-sources",
        headers=_auth(_login(client, account_user.username)),
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "plugin_id": "actor_source",
            "display_name": "Actor Source",
            "supports_actor_movies": True,
        },
        {
            "plugin_id": "plain_source",
            "display_name": "Plain Source",
            "supports_actor_movies": False,
        },
    ]
