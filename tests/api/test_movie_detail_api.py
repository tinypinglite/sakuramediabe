from types import SimpleNamespace

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_movie_detail_exposes_media_playback_deliveries(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(name="detail-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="DETAIL-001", javdb_id="detail-1", title="detail")
    media = Media.create(movie=movie, library=library, file_name="detail.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.json()["media_items"][0]["media_id"] == media.id
    assert response.json()["media_items"][0]["playback_deliveries"] == ["proxy", "redirect"]
