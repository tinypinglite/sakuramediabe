from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _invalid_media(*, number: str, reason: str) -> Media:
    library = MediaLibrary.create(
        name=f"{number}-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(
        movie_number=number,
        javdb_id=f"javdb-{number}",
        title=number,
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        valid=False,
        playback_health={
            "version": 1,
            "status": "invalid",
            "reason": reason,
            "detail": {"max_gap_mib": 6920.8, "max_gap_percent": 95},
            "checked_at": "2026-11-05T04:00:00+00:00",
        },
    )


def test_invalid_media_list_exposes_reason_and_detail(
    client,
    test_db,
    account_user,
):
    media = _invalid_media(number="INVALID-001", reason="interleave_bad")

    response = client.get(
        "/media/invalid",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == media.id
    assert item["invalid_reason"] == "interleave_bad"
    assert item["invalid_detail"]["max_gap_mib"] == 6920.8
    assert item["invalid_checked_at"] == "2026-11-05T04:00:00+00:00"


def test_invalid_media_cannot_be_played_even_with_valid_signature(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
):
    media = _invalid_media(number="INVALID-002", reason="moov_missing")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: (_ for _ in ()).throw(
            AssertionError("invalid media must not reach provider")
        ),
    )

    response = client.get(build_signed_media_url(media.id))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_invalid"


def test_delete_structural_invalid_media_calls_provider(
    client,
    test_db,
    account_user,
    monkeypatch,
):
    media = _invalid_media(number="INVALID-003", reason="interleave_bad")
    deleted = []

    class Storage:
        @staticmethod
        def delete_media(*, media):
            deleted.append(media.media_id)

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: Storage(),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 204
    assert deleted == [media.id]
    assert Media.get_or_none(Media.id == media.id) is None


def test_delete_missing_media_skips_provider(
    client,
    test_db,
    account_user,
    monkeypatch,
):
    media = _invalid_media(number="INVALID-004", reason="file_missing")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: (_ for _ in ()).throw(
            AssertionError("missing media must not reach provider")
        ),
    )

    response = client.delete(
        f"/media/{media.id}",
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 204
    assert Media.get_or_none(Media.id == media.id) is None
