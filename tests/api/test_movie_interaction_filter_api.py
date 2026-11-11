"""影片互动数范围筛选与排序。"""

import pytest

from src.model import Movie, MovieTag, Tag


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(number: str, **interactions) -> Movie:
    return Movie.create(
        movie_number=number,
        title=number,
        **interactions,
    )


def _item_numbers(body: dict) -> list[str]:
    return [item["movie_number"] for item in body["items"]]


@pytest.fixture
def interaction_movies():
    return {
        "high": _create_movie(
            "INT-HIGH",
            score=9.2,
            score_number=5000,
            watched_count=1000,
            want_watch_count=200,
            comment_count=50,
            heat=8000,
        ),
        "middle": _create_movie(
            "INT-MIDDLE",
            score=8.8,
            score_number=3000,
            watched_count=500,
            want_watch_count=100,
            comment_count=20,
            heat=3000,
        ),
        "low": _create_movie(
            "INT-LOW",
            score=7.5,
            score_number=100,
            watched_count=10,
            want_watch_count=5,
            comment_count=2,
            heat=500,
        ),
    }


def test_movie_interaction_ranges_and_sort(client, account_user, interaction_movies):
    headers = _login(client, account_user.username)

    response = client.get(
        "/movies",
        headers=headers,
        params={
            "score_min": 8,
            "comment_count_min": 20,
            "sort": "watched_count:desc",
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert _item_numbers(response.json()) == ["INT-HIGH", "INT-MIDDLE"]

    sorted_response = client.get(
        "/movies",
        headers=headers,
        params={"sort": "score:desc"},
    )
    assert _item_numbers(sorted_response.json()) == [
        "INT-HIGH",
        "INT-MIDDLE",
        "INT-LOW",
    ]

    bounded = client.get(
        "/movies",
        headers=headers,
        params={"score_max": 7.6, "watched_count_max": 10},
    )
    assert _item_numbers(bounded.json()) == ["INT-LOW"]


@pytest.mark.parametrize(
    "params",
    [
        {"heat_min": 10, "heat_max": 5},
        {"score_min": 9, "score_max": 8},
        {"score_number_min": 10, "score_number_max": 5},
        {"watched_count_min": 10, "watched_count_max": 5},
        {"want_watch_count_min": 10, "want_watch_count_max": 5},
        {"comment_count_min": 10, "comment_count_max": 5},
    ],
)
def test_movie_interaction_range_rejects_min_greater_than_max(
    client, account_user, params
):
    headers = _login(client, account_user.username)

    response = client.get("/movies", headers=headers, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_movie_interaction_filter_applies_to_tag_movies(client, account_user):
    headers = _login(client, account_user.username)
    tag = Tag.create(name="interaction-filter")
    high = _create_movie("TAG-INT-HIGH", score=9.0, comment_count=30)
    low = _create_movie("TAG-INT-LOW", score=6.0, comment_count=1)
    MovieTag.create(movie=high, tag=tag)
    MovieTag.create(movie=low, tag=tag)

    response = client.get(
        f"/tags/{tag.id}/movies",
        headers=headers,
        params={"score_min": 8, "sort": "comment_count:desc"},
    )

    assert response.status_code == 200
    assert _item_numbers(response.json()) == ["TAG-INT-HIGH"]


def test_interaction_filter_options_computes_current_distribution(
    client, account_user
):
    headers = _login(client, account_user.username)
    _create_movie(
        "DIST-001",
        heat=100,
        score=4.0,
        score_number=10,
        watched_count=1,
        want_watch_count=2,
        comment_count=3,
    )
    _create_movie(
        "DIST-002",
        heat=200,
        score=5.0,
        score_number=20,
        watched_count=2,
        want_watch_count=4,
        comment_count=6,
    )
    _create_movie(
        "DIST-BLACKLISTED",
        heat=99999,
        score=5.0,
        score_number=99999,
        watched_count=99999,
        want_watch_count=99999,
        comment_count=99999,
        is_blacklisted=True,
    )

    response = client.get("/movies/interaction-filter-options", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["movie_count"] == 2
    assert "p99" not in body["heat"]
    assert body["heat"]["p999"] == 200
    assert body["heat"]["max"] == 200
    assert body["score"]["max"] == 5.0
    assert body["comment_count"]["p999"] == 6
    assert body["comment_count"]["max"] == 6

    _create_movie(
        "DIST-003",
        heat=300,
        score=5.0,
        score_number=30,
        watched_count=3,
        want_watch_count=6,
        comment_count=9,
    )
    refreshed = client.get("/movies/interaction-filter-options", headers=headers)
    assert refreshed.json()["heat"]["max"] == 300
    assert refreshed.json()["movie_count"] == 3


def test_movie_interaction_sort_rejects_unknown_field(client, account_user):
    headers = _login(client, account_user.username)

    response = client.get(
        "/movies",
        headers=headers,
        params={"sort": "favorite_count:desc"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"
