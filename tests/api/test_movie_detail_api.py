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


def test_movie_detail_exposes_merge_playback_candidate_for_supported_library(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(name="merge-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="MERGE-001", javdb_id="merge-1", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert [item["media_id"] for item in response.json()["media_items"]] == [first.id, second.id]
    assert response.json()["merge_playback_candidates"] == [
        {
            "library_id": library.id,
            "library_name": "merge-library",
            "provider_key": "demo",
            "segment_count": 2,
        }
    ]


def test_merged_playback_url_uses_ordered_same_library_media(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-url-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="MERGE-URL-001", javdb_id="merge-url-1", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    response = client.get(
        f"/movies/{movie.movie_number}/merged-playback?library_id={library.id}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert (
        f"/media/merged-play/stream.mp4?media_ids={first.id},{second.id}"
        in response.json()["play_url"]
    )
