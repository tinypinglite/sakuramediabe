from src.model import HotReviewItem, Image, Media, Movie


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_hot_reviews_router_requires_authentication(client):
    response = client.get("/hot-reviews")

    assert response.status_code == 401


def test_list_hot_reviews_returns_paginated_items(client, account_user):
    token = _login(client, username=account_user.username)
    thin_cover_image = Image.create(
        origin="hot-review/thin-origin.jpg",
        small="hot-review/thin-small.jpg",
        medium="hot-review/thin-medium.jpg",
        large="hot-review/thin-large.jpg",
    )
    movie_a = _create_movie(
        "ABP-001",
        "javdb-abp001",
        title="Movie A",
        title_zh="热评中文 A",
        thin_cover_image=thin_cover_image,
    )
    movie_b = _create_movie("ABP-002", "javdb-abp002", title="Movie B")
    movie_c = _create_movie("ABP-003", "javdb-abp003", title="Movie C")
    Media.create(movie=movie_b, path="/library/main/abp-002.mp4", valid=True)

    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=2,
        review_id=102,
        movie_number=movie_b.movie_number,
        movie=movie_b,
        score=4,
        content="good",
        review_created_at="2026-03-21T02:00:00Z",
        username="user-b",
        like_count=12,
        watch_count=22,
    )
    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=1,
        review_id=101,
        movie_number=movie_a.movie_number,
        movie=movie_a,
        score=5,
        content="great",
        review_created_at="2026-03-21T01:00:00Z",
        username="user-a",
        like_count=11,
        watch_count=21,
    )
    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=3,
        review_id=103,
        movie_number=movie_c.movie_number,
        movie=movie_c,
        score=3,
        content="ok",
        review_created_at="2026-03-21T03:00:00Z",
        username="user-c",
        like_count=13,
        watch_count=23,
    )

    response = client.get(
        "/hot-reviews?period=weekly&page=1&page_size=2",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert payload["total"] == 3
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert [item["review_id"] for item in payload["items"]] == [101, 102]
    assert [item["movie"]["movie_number"] for item in payload["items"]] == ["ABP-001", "ABP-002"]
    assert payload["items"][0]["movie"]["can_play"] is False
    assert payload["items"][1]["movie"]["can_play"] is True
    assert payload["items"][0]["movie"]["title_zh"] == "热评中文 A"
    assert payload["items"][0]["movie"]["thin_cover_image"]["id"] == thin_cover_image.id
    assert payload["items"][1]["movie"]["thin_cover_image"] is None
    assert [item["created_at"] for item in payload["items"]] == [
        "2026-03-21T01:00:00",
        "2026-03-21T02:00:00",
    ]


def test_list_hot_reviews_validates_period(client, account_user):
    token = _login(client, username=account_user.username)
    response = client.get(
        "/hot-reviews?period=daily",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_hot_review_period"
