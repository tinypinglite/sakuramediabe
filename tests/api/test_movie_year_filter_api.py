"""影片列表 year 多选筛选：CSV 参数解析与 OR 语义。"""

from datetime import datetime

from src.model import Movie, MovieTag, Tag


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(number: str, release_date: datetime) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{number}",
        movie_number=number,
        title=number,
        release_date=release_date,
    )


def _item_numbers(body: dict) -> set[str]:
    return {item["movie_number"] for item in body["items"]}


def test_movie_year_filter_accepts_single_and_csv(client, account_user):
    headers = _login(client, account_user.username)
    _create_movie("YEAR-2024", datetime(2024, 12, 31))
    _create_movie("YEAR-2024B", datetime(2024, 1, 1))
    _create_movie("YEAR-2023", datetime(2023, 1, 1))
    _create_movie("YEAR-2022", datetime(2022, 6, 1))

    single = client.get("/movies", headers=headers, params={"year": "2024"})
    assert single.status_code == 200
    assert single.json()["total"] == 2
    assert _item_numbers(single.json()) == {"YEAR-2024", "YEAR-2024B"}

    multi = client.get("/movies", headers=headers, params={"year": "2023,2024"})
    assert multi.status_code == 200
    assert multi.json()["total"] == 3
    assert _item_numbers(multi.json()) == {"YEAR-2024", "YEAR-2024B", "YEAR-2023"}

    no_filter = client.get("/movies", headers=headers)
    assert no_filter.json()["total"] == 4


def test_tag_movies_year_filter_accepts_csv(client, account_user):
    headers = _login(client, account_user.username)
    tag = Tag.create(name="year-filter")
    tagged_2024 = _create_movie("TAGYEAR-2024", datetime(2024, 5, 1))
    tagged_2023 = _create_movie("TAGYEAR-2023", datetime(2023, 5, 1))
    _create_movie("TAGYEAR-2022", datetime(2022, 5, 1))
    MovieTag.create(movie=tagged_2024, tag=tag)
    MovieTag.create(movie=tagged_2023, tag=tag)

    response = client.get(
        f"/tags/{tag.id}/movies",
        headers=headers,
        params={"year": "2023,2024"},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert _item_numbers(response.json()) == {"TAGYEAR-2024", "TAGYEAR-2023"}


def test_movie_year_filter_rejects_invalid_csv(client, account_user):
    headers = _login(client, account_user.username)
    for raw in ("2023,abc", "0", "2023,", "9999"):
        response = client.get("/movies", headers=headers, params={"year": raw})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_movie_filter"
