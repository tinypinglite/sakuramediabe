import pytest

from src.model import Media, MediaLibrary, Movie


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_media(
    library: MediaLibrary,
    movie: Movie,
    *,
    file_name: str,
    playback_health: dict | None,
) -> Media:
    return Media.create(
        movie=movie,
        library=library,
        file_name=file_name,
        playback_health=playback_health,
    )


def _media_by_status() -> dict[str, dict | None]:
    checked_at = "2026-11-05T04:00:00+00:00"
    return {
        "ok": {
            "status": "ok",
            "reason": None,
            "detail": {
                "max_gap_mib": 8.2,
                "threshold_mib": 64.0,
            },
            "checked_at": checked_at,
        },
        "unknown": {
            "status": "unknown",
            "reason": None,
            "detail": {"error_type": "provider_unavailable"},
            "checked_at": checked_at,
        },
        "invalid": {
            "status": "invalid",
            "reason": "interleave_bad",
            "detail": {
                "max_gap_mib": 1024.0,
                "threshold_mib": 64.0,
            },
            "checked_at": checked_at,
        },
        "not_checked": None,
        "malformed": {"status": "legacy"},
        "empty": {},
    }


@pytest.mark.parametrize(
    ("status", "expected_keys"),
    [
        (
            None,
            {"ok", "unknown", "invalid", "not_checked", "malformed", "empty"},
        ),
        ("ok", {"ok"}),
        ("unknown", {"unknown"}),
        ("invalid", {"invalid"}),
        ("not_checked", {"not_checked", "malformed", "empty"}),
    ],
)
def test_media_list_filters_playback_health_status(
    client,
    test_db,
    account_user,
    status,
    expected_keys,
):
    library = MediaLibrary.create(
        name="playback-health-filter",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="PLAYBACK-HEALTH-001",
        javdb_id="playback-health-1",
        title="playback health",
    )
    health_by_key = _media_by_status()
    media_by_key = {
        key: _create_media(
            library,
            movie,
            file_name=f"{key}.mp4",
            playback_health=health,
        )
        for key, health in health_by_key.items()
    }

    params = {}
    if status is not None:
        params["playback_health_status"] = status
    response = client.get(
        "/media",
        params={**params, "page_size": 20},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    expected_ids = {media_by_key[key].id for key in expected_keys}
    assert body["total"] == len(expected_keys)
    assert {item["id"] for item in body["items"]} == expected_ids

    for key in expected_keys:
        item = next(
            item for item in body["items"] if item["id"] == media_by_key[key].id
        )
        health = health_by_key[key]
        expected_status = {
            "ok": "ok",
            "unknown": "unknown",
            "invalid": "invalid",
        }.get(key, "not_checked")
        assert item["playback_health"]["status"] == expected_status
        if health and health.get("status") in {"ok", "unknown", "invalid"}:
            assert item["playback_health"]["reason"] == health["reason"]
            assert item["playback_health"]["detail"] == health["detail"]
            assert item["playback_health"]["checked_at"] == health["checked_at"]
        else:
            assert item["playback_health"]["reason"] is None
            assert item["playback_health"]["detail"] == {}
            assert item["playback_health"]["checked_at"] is None
