"""影片热度范围筛选与排序。"""

import pytest

from src.model import Movie, MovieTag, Tag


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(number: str, *, heat: int, **kwargs) -> Movie:
    return Movie.create(
        movie_number=number,
        title=number,
        heat=heat,
        **kwargs,
    )


def _item_numbers(body: dict) -> list[str]:
    return [item["movie_number"] for item in body["items"]]


@pytest.fixture
def interaction_movies():
    return {
        "high": _create_movie("INT-HIGH", heat=8000),
        "middle": _create_movie("INT-MIDDLE", heat=3000),
        "low": _create_movie("INT-LOW", heat=500),
    }


def test_movie_heat_range_and_sort(client, account_user, interaction_movies):
    headers = _login(client, account_user.username)

    response = client.get(
        "/movies",
        headers=headers,
        params={"heat_min": 3000, "sort": "heat:desc"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert _item_numbers(response.json()) == ["INT-HIGH", "INT-MIDDLE"]

    bounded = client.get(
        "/movies",
        headers=headers,
        params={"heat_max": 500, "sort": "heat:asc"},
    )
    assert _item_numbers(bounded.json()) == ["INT-LOW"]


def test_movie_heat_range_rejects_min_greater_than_max(client, account_user):
    headers = _login(client, account_user.username)

    response = client.get(
        "/movies",
        headers=headers,
        params={"heat_min": 10, "heat_max": 5},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_movie_heat_filter_applies_to_tag_movies(client, account_user):
    headers = _login(client, account_user.username)
    tag = Tag.create(name="heat-filter")
    high = _create_movie("TAG-INT-HIGH", heat=3000)
    low = _create_movie("TAG-INT-LOW", heat=100)
    MovieTag.create(movie=high, tag=tag)
    MovieTag.create(movie=low, tag=tag)

    response = client.get(
        f"/tags/{tag.id}/movies",
        headers=headers,
        params={"heat_min": 1000, "sort": "heat:desc"},
    )

    assert response.status_code == 200
    assert _item_numbers(response.json()) == ["TAG-INT-HIGH"]


def test_heat_filter_options_only_exposes_heat(client, account_user):
    headers = _login(client, account_user.username)
    _create_movie("DIST-001", heat=100)
    _create_movie("DIST-002", heat=200)
    _create_movie("DIST-BLACKLISTED", heat=99999, is_blacklisted=True)

    response = client.get("/movies/interaction-filter-options", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["movie_count"] == 2
    assert body["heat"]["p999"] == 200
    assert body["heat"]["max"] == 200
    assert "score" not in body
    assert "comment_count" not in body

    _create_movie("DIST-003", heat=300)
    refreshed = client.get("/movies/interaction-filter-options", headers=headers)
    assert refreshed.json()["heat"]["max"] == 300
    assert refreshed.json()["movie_count"] == 3


def test_movie_heat_sort_rejects_unknown_field(client, account_user):
    headers = _login(client, account_user.username)

    response = client.get(
        "/movies",
        headers=headers,
        params={"sort": "favorite_count:desc"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"
