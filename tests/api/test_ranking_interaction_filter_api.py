"""排行榜影片互动数筛选与排序。"""

from src.model import Movie, RankingItem
from src.service.discovery import ranking_service
from src.service.discovery.ranking_service import (
    RankingBoardDefinition,
    RankingSourceDefinition,
)


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _register_board(monkeypatch) -> None:
    monkeypatch.setitem(
        ranking_service.RANKING_SOURCES,
        "test",
        RankingSourceDefinition(
            key="test",
            name="Test",
            boards=(RankingBoardDefinition(key="all", name="总榜"),),
        ),
    )


def _create_movie(number: str, rank: int, **interactions) -> Movie:
    movie = Movie.create(
        movie_number=number,
        title=number,
        **interactions,
    )
    RankingItem.create(
        source_key="test",
        board_key="all",
        period="",
        rank=rank,
        movie=movie,
        movie_number=movie.movie_number,
    )
    return movie


def _item_numbers(body: dict) -> list[str]:
    return [item["movie_number"] for item in body["items"]]


def test_ranking_items_filter_and_sort_by_interaction(client, account_user, monkeypatch):
    headers = _login(client, account_user.username)
    _register_board(monkeypatch)
    _create_movie(
        "RANK-HIGH",
        1,
        score=9.0,
        score_number=5000,
        watched_count=1000,
        want_watch_count=200,
        comment_count=50,
        heat=8000,
    )
    _create_movie(
        "RANK-MIDDLE",
        2,
        score=8.5,
        score_number=3000,
        watched_count=500,
        want_watch_count=100,
        comment_count=20,
        heat=3000,
    )
    _create_movie(
        "RANK-LOW",
        3,
        score=6.0,
        score_number=100,
        watched_count=10,
        want_watch_count=5,
        comment_count=2,
        heat=500,
    )

    filtered = client.get(
        "/ranking-sources/test/boards/all/items",
        headers=headers,
        params={"score_min": 8, "sort": "watched_count:desc"},
    )

    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert _item_numbers(filtered.json()) == ["RANK-HIGH", "RANK-MIDDLE"]

    sorted_by_comment = client.get(
        "/ranking-sources/test/boards/all/items",
        headers=headers,
        params={"sort": "comment_count:desc"},
    )
    assert _item_numbers(sorted_by_comment.json()) == [
        "RANK-HIGH",
        "RANK-MIDDLE",
        "RANK-LOW",
    ]


def test_ranking_items_reject_invalid_interaction_sort_and_range(
    client, account_user, monkeypatch
):
    headers = _login(client, account_user.username)
    _register_board(monkeypatch)

    invalid_sort = client.get(
        "/ranking-sources/test/boards/all/items",
        headers=headers,
        params={"sort": "favorite_count:desc"},
    )
    assert invalid_sort.status_code == 422
    assert invalid_sort.json()["error"]["code"] == "invalid_ranking_filter"

    invalid_range = client.get(
        "/ranking-sources/test/boards/all/items",
        headers=headers,
        params={"score_min": 9, "score_max": 8},
    )
    assert invalid_range.status_code == 422
    assert invalid_range.json()["error"]["code"] == "invalid_ranking_filter"
