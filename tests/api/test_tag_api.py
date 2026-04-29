from datetime import datetime

from src.model import Movie, MovieTag, Tag


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


def test_list_tags_requires_auth(client):
    response = client.get("/tags")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_tags_returns_movie_counts_and_default_sort(client, account_user):
    token = _login(client, username=account_user.username)
    plot_tag = Tag.create(name="剧情")
    uniform_tag = Tag.create(name="制服")
    vr_tag = Tag.create(name="VR")
    first_movie = _create_movie("ABP-120", "MovieA1")
    second_movie = _create_movie("ABP-121", "MovieA2")
    MovieTag.create(movie=first_movie, tag=plot_tag)
    MovieTag.create(movie=second_movie, tag=plot_tag)
    MovieTag.create(movie=first_movie, tag=uniform_tag)

    response = client.get("/tags", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == [
        {"tag_id": plot_tag.id, "name": "剧情", "movie_count": 2},
        {"tag_id": uniform_tag.id, "name": "制服", "movie_count": 1},
        {"tag_id": vr_tag.id, "name": "VR", "movie_count": 0},
    ]


def test_list_tags_supports_query_and_name_sort(client, account_user):
    token = _login(client, username=account_user.username)
    first_tag = Tag.create(name="剧情")
    second_tag = Tag.create(name="剧情向")
    Tag.create(name="制服")

    response = client.get(
        "/tags?query=剧情&sort=name:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {"tag_id": second_tag.id, "name": "剧情向", "movie_count": 0},
        {"tag_id": first_tag.id, "name": "剧情", "movie_count": 0},
    ]


def test_list_tags_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/tags?sort=id:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_tag_filter"


def test_get_tag_returns_movie_count(client, account_user):
    token = _login(client, username=account_user.username)
    tag = Tag.create(name="剧情")
    movie = _create_movie("ABP-120", "MovieA1")
    MovieTag.create(movie=movie, tag=tag)

    response = client.get(f"/tags/{tag.id}", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"tag_id": tag.id, "name": "剧情", "movie_count": 1}


def test_get_tag_returns_404_when_missing(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get("/tags/404", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tag_not_found"


def test_list_tag_movies_returns_paginated_movies(client, account_user):
    token = _login(client, username=account_user.username)
    tag = Tag.create(name="剧情")
    first_movie = _create_movie(
        "ABP-120",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    second_movie = _create_movie(
        "ABP-121",
        "MovieA2",
        title="Movie 2",
        release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    _create_movie("ABP-122", "MovieA3", title="Movie 3")
    MovieTag.create(movie=first_movie, tag=tag)
    MovieTag.create(movie=second_movie, tag=tag)

    response = client.get(
        f"/tags/{tag.id}/movies?sort=release_date:desc&page=1&page_size=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 1
    assert [item["movie_number"] for item in body["items"]] == ["ABP-121"]


def test_list_tag_movies_supports_director_and_maker_exact_filters(client, account_user):
    token = _login(client, username=account_user.username)
    tag = Tag.create(name="剧情")
    target_movie = _create_movie(
        "ABP-120",
        "MovieA1",
        title="Movie 1",
        director_name="嵐山みちる",
        maker_name="S1 NO.1 STYLE",
    )
    same_director_movie = _create_movie(
        "ABP-121",
        "MovieA2",
        title="Movie 2",
        director_name="嵐山みちる",
        maker_name="MOODYZ",
    )
    same_maker_movie = _create_movie(
        "ABP-122",
        "MovieA3",
        title="Movie 3",
        director_name="別导演",
        maker_name="S1 NO.1 STYLE",
    )
    MovieTag.create(movie=target_movie, tag=tag)
    MovieTag.create(movie=same_director_movie, tag=tag)
    MovieTag.create(movie=same_maker_movie, tag=tag)

    response = client.get(
        f"/tags/{tag.id}/movies?director_name=嵐山みちる&maker_name=S1%20NO.1%20STYLE",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == ["ABP-120"]


def test_list_tag_movies_rejects_blank_director_or_maker_filter(client, account_user):
    token = _login(client, username=account_user.username)
    tag = Tag.create(name="剧情")

    director_response = client.get(
        f"/tags/{tag.id}/movies?director_name=%20%20%20",
        headers={"Authorization": f"Bearer {token}"},
    )
    maker_response = client.get(
        f"/tags/{tag.id}/movies?maker_name=%20%20%20",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert director_response.status_code == 422
    assert director_response.json()["error"]["code"] == "invalid_movie_filter"
    assert maker_response.status_code == 422
    assert maker_response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_tag_movies_returns_404_when_missing(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get("/tags/404/movies", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tag_not_found"
