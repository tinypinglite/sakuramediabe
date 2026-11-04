from types import SimpleNamespace

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _media(
    library: MediaLibrary,
    movie: Movie,
    *,
    file_name: str,
    gap_mib: float | None,
) -> Media:
    playback_health = (
        None
        if gap_mib is None
        else {
            "status": "ok",
            "reason": None,
            "detail": {"max_gap_mib": gap_mib},
            "checked_at": "2026-11-05T00:00:00+00:00",
        }
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name=file_name,
        playback_health=playback_health,
    )


def test_media_list_filters_by_interleave_gap(client, account_user):
    library = MediaLibrary.create(
        name="gap-filter-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="GAP-FILTER-001",
        javdb_id="gap-filter-1",
        title="gap filter",
    )
    below = _media(library, movie, file_name="below.mp4", gap_mib=63.9)
    at_threshold = _media(library, movie, file_name="at.mp4", gap_mib=64.0)
    above = _media(library, movie, file_name="above.mp4", gap_mib=256.0)
    missing = _media(library, movie, file_name="missing.mp4", gap_mib=None)

    response = client.get(
        "/media",
        params={"interleave_gap_mib_min": 64},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {at_threshold.id, above.id}
    assert all(item["interleave_gap_mib"] >= 64 for item in body["items"])
    assert below.id not in {item["id"] for item in body["items"]}
    assert missing.id not in {item["id"] for item in body["items"]}


def test_movie_detail_exposes_interleave_gap(client, account_user, monkeypatch):
    library = MediaLibrary.create(
        name="gap-detail-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="GAP-DETAIL-001",
        javdb_id="gap-detail-1",
        title="gap detail",
    )
    media = _media(library, movie, file_name="gap-detail.mp4", gap_mib=128.5)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    item = response.json()["media_items"][0]
    assert item["media_id"] == media.id
    assert item["interleave_gap_mib"] == 128.5


def test_movie_detail_returns_null_interleave_gap_when_missing(
    client, account_user, monkeypatch
):
    library = MediaLibrary.create(
        name="gap-missing-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="GAP-MISSING-001",
        javdb_id="gap-missing-1",
        title="gap missing",
    )
    _media(library, movie, file_name="gap-missing.mp4", gap_mib=None)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    assert response.json()["media_items"][0]["interleave_gap_mib"] is None
