from src.model import Media, MediaLibrary, Movie, MovieRating, RatingDimension


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth_headers(client, account_user) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, account_user.username)}"}


def _create_movie(movie_number: str, **fields) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        title=f"title-{movie_number}",
        **fields,
    )


def _create_dimension(
    client,
    headers,
    name: str,
    *,
    weight: float = 1.0,
    max_value: int | None = None,
) -> dict:
    payload = {"name": name, "weight": weight}
    if max_value is not None:
        payload["max_value"] = max_value
    response = client.post(
        "/movie-rating-dimensions",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _save_rating(client, headers, movie_number: str, values: dict[int, int]):
    return client.put(
        f"/movies/{movie_number}/rating",
        headers=headers,
        json={"values": values},
    )


def test_rating_dimension_crud(client, account_user):
    headers = _auth_headers(client, account_user)
    looks = _create_dimension(client, headers, "颜值")
    plot = _create_dimension(client, headers, "剧情")

    assert looks["min_value"] == 1
    assert looks["max_value"] == 5
    assert looks["has_scores"] is False
    listed = client.get("/movie-rating-dimensions", headers=headers)
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["颜值", "剧情"]

    duplicate = client.post(
        "/movie-rating-dimensions",
        headers=headers,
        json={"name": "颜值"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "rating_dimension_exists"

    blank = client.post(
        "/movie-rating-dimensions",
        headers=headers,
        json={"name": "   "},
    )
    assert blank.status_code == 422

    updated = client.put(
        f"/movie-rating-dimensions/{looks['id']}",
        headers=headers,
        json={"name": "颜值 2"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "颜值 2"
    assert updated.json()["min_value"] == 1
    assert updated.json()["max_value"] == 5

    assert (
        client.delete(
            f"/movie-rating-dimensions/{plot['id']}", headers=headers
        ).status_code
        == 204
    )
    assert len(client.get("/movie-rating-dimensions", headers=headers).json()) == 1

    defaulted = client.post(
        "/movie-rating-dimensions",
        headers=headers,
        json={"name": "默认权重"},
    )
    assert defaulted.status_code == 201
    assert defaulted.json()["weight"] == 1.0


def test_rating_dimension_scale_options_and_validation(client, account_user):
    headers = _auth_headers(client, account_user)

    ten = _create_dimension(client, headers, "十分制", max_value=10)
    hundred = _create_dimension(client, headers, "百分制", max_value=100)
    assert (ten["min_value"], ten["max_value"]) == (1, 10)
    assert (hundred["min_value"], hundred["max_value"]) == (1, 100)

    invalid = client.post(
        "/movie-rating-dimensions",
        headers=headers,
        json={"name": "非法范围", "max_value": 7},
    )
    assert invalid.status_code == 422

    renamed = client.put(
        f"/movie-rating-dimensions/{ten['id']}",
        headers=headers,
        json={"name": "十分制（改名）"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["max_value"] == 10

    _create_movie("RT-SCALE")
    assert _save_rating(client, headers, "RT-SCALE", {ten["id"]: 10}).status_code == 200
    assert (
        _save_rating(client, headers, "RT-SCALE", {ten["id"]: 11}).json()["error"][
            "code"
        ]
        == "rating_value_out_of_range"
    )
    assert (
        _save_rating(client, headers, "RT-SCALE", {hundred["id"]: 100}).status_code
        == 200
    )
    assert (
        _save_rating(client, headers, "RT-SCALE", {hundred["id"]: 101}).json()[
            "error"
        ]["code"]
        == "rating_value_out_of_range"
    )


def test_dimension_scale_locks_after_scores_exist(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-LOCK")
    dimension = _create_dimension(client, headers, "锁定范围", max_value=10)

    assert _save_rating(client, headers, "RT-LOCK", {dimension["id"]: 8}).status_code == 200
    listed = client.get("/movie-rating-dimensions", headers=headers).json()
    assert listed[0]["has_scores"] is True

    locked = client.put(
        f"/movie-rating-dimensions/{dimension['id']}",
        headers=headers,
        json={"name": "锁定范围", "max_value": 100},
    )
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "rating_dimension_scale_locked"

    renamed = client.put(
        f"/movie-rating-dimensions/{dimension['id']}",
        headers=headers,
        json={"name": "改名后", "max_value": 10},
    )
    assert renamed.status_code == 200
    assert renamed.json()["has_scores"] is True
    assert renamed.json()["max_value"] == 10

    assert client.delete("/movies/RT-LOCK/rating", headers=headers).status_code == 204
    unlocked = client.put(
        f"/movie-rating-dimensions/{dimension['id']}",
        headers=headers,
        json={"name": "改名后", "max_value": 100},
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["max_value"] == 100
    assert unlocked.json()["has_scores"] is False


def test_movie_rating_mixed_scales_normalize_total_score(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-MIXED")
    five = _create_dimension(client, headers, "五分")
    ten = _create_dimension(client, headers, "十分", max_value=10)
    hundred = _create_dimension(client, headers, "百分", max_value=100)

    response = _save_rating(
        client,
        headers,
        "RT-MIXED",
        {five["id"]: 5, ten["id"]: 5, hundred["id"]: 100},
    )
    assert response.status_code == 200
    assert response.json()["total_score"] == 4.3


def test_movie_rating_total_score_and_partial_values(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-001")
    looks = _create_dimension(client, headers, "颜值")
    plot = _create_dimension(client, headers, "剧情")

    response = _save_rating(
        client, headers, "RT-001", {looks["id"]: 4, plot["id"]: 3}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rated"] is True
    assert payload["total_score"] == 3.5
    assert [item["dimension_id"] for item in payload["values"]] == [
        looks["id"],
        plot["id"],
    ]

    partial = _save_rating(client, headers, "RT-001", {looks["id"]: 4})
    assert partial.status_code == 200
    assert partial.json()["total_score"] == 4.0

    out_of_range = _save_rating(client, headers, "RT-001", {looks["id"]: 6})
    assert out_of_range.status_code == 422
    assert out_of_range.json()["error"]["code"] == "rating_value_out_of_range"

    assert _save_rating(client, headers, "RT-404", {looks["id"]: 3}).status_code == 404

    rating = client.get("/movies/RT-001/rating", headers=headers)
    assert rating.status_code == 200
    assert rating.json()["total_score"] == 4.0
    assert len(rating.json()["values"]) == 1

    assert (
        client.delete("/movies/RT-001/rating", headers=headers).status_code == 204
    )
    assert client.get("/movies/RT-001/rating", headers=headers).json()["rated"] is False


def test_movie_rating_weight_and_delete_dimension(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-002")
    looks = _create_dimension(client, headers, "颜值", weight=3.0)
    plot = _create_dimension(client, headers, "剧情", weight=1.0)

    response = _save_rating(
        client, headers, "RT-002", {looks["id"]: 5, plot["id"]: 1}
    )
    assert response.status_code == 200
    # (3×5 + 1×1) / 4 = 4.0
    assert response.json()["total_score"] == 4.0

    assert (
        client.delete(
            f"/movie-rating-dimensions/{looks['id']}", headers=headers
        ).status_code
        == 204
    )
    rating = client.get("/movies/RT-002/rating", headers=headers)
    assert rating.json()["total_score"] == 1.0
    assert [item["dimension_id"] for item in rating.json()["values"]] == [
        plot["id"]
    ]

    assert (
        client.delete(
            f"/movie-rating-dimensions/{plot['id']}", headers=headers
        ).status_code
        == 204
    )
    assert MovieRating.select().count() == 0
    assert client.get("/movies/RT-002/rating", headers=headers).json()["rated"] is False


def test_movie_rating_total_score_rounds_half_up(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-003")
    dimensions = [
        _create_dimension(client, headers, name)
        for name in ("维度1", "维度2", "维度3", "维度4")
    ]

    response = _save_rating(
        client,
        headers,
        "RT-003",
        {
            dimension["id"]: value
            for dimension, value in zip(dimensions, (4, 3, 3, 3))
        },
    )

    assert response.status_code == 200
    # (4+3+3+3)/4 = 3.25，与前端预览一致取 3.3。
    assert response.json()["total_score"] == 3.3


def test_rename_dimension_keeps_weight_and_scores(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-004")
    looks = _create_dimension(client, headers, "颜值", weight=3.0)
    plot = _create_dimension(client, headers, "剧情")
    assert (
        _save_rating(
            client, headers, "RT-004", {looks["id"]: 5, plot["id"]: 1}
        ).status_code
        == 200
    )

    renamed = client.put(
        f"/movie-rating-dimensions/{looks['id']}",
        headers=headers,
        json={"name": "颜值2"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["weight"] == 3.0
    assert (
        client.get("/movies/RT-004/rating", headers=headers).json()["total_score"]
        == 4.0
    )

    reweighted = client.put(
        f"/movie-rating-dimensions/{looks['id']}",
        headers=headers,
        json={"name": "颜值2", "weight": 1.0},
    )
    assert reweighted.status_code == 200
    # 等权重后 (5+1)/2 = 3.0。
    assert (
        client.get("/movies/RT-004/rating", headers=headers).json()["total_score"]
        == 3.0
    )


def test_movie_list_filters_rated_and_sorts_by_score(client, account_user):
    headers = _auth_headers(client, account_user)
    _create_movie("RT-101")
    _create_movie("RT-102")
    _create_movie("RT-103")
    looks = _create_dimension(client, headers, "颜值")
    plot = _create_dimension(client, headers, "剧情")

    assert _save_rating(client, headers, "RT-101", {looks["id"]: 5}).status_code == 200
    assert (
        _save_rating(client, headers, "RT-102", {looks["id"]: 4, plot["id"]: 2})
        .status_code
        == 200
    )

    rated = client.get("/movies?rated=true", headers=headers)
    assert rated.status_code == 200
    assert rated.json()["total"] == 2
    scores = {
        item["movie_number"]: item["my_score"] for item in rated.json()["items"]
    }
    assert scores == {"RT-101": 5.0, "RT-102": 3.0}
    values = {
        item["movie_number"]: item["my_rating_values"]
        for item in rated.json()["items"]
    }
    assert values == {
        "RT-101": {str(looks["id"]): 5},
        "RT-102": {str(looks["id"]): 4, str(plot["id"]): 2},
    }

    by_total = client.get(
        "/movies?rated=true&sort=my_score:desc", headers=headers
    )
    assert [item["movie_number"] for item in by_total.json()["items"]] == [
        "RT-101",
        "RT-102",
    ]

    by_dimension = client.get(
        f"/movies?rated=true&sort=dimension_{plot['id']}:desc",
        headers=headers,
    )
    assert by_dimension.status_code == 200
    # RT-102 的剧情是 2，RT-101 没有剧情分（NULLS LAST）。
    assert [item["movie_number"] for item in by_dimension.json()["items"]] == [
        "RT-102",
        "RT-101",
    ]

    library = client.get("/movies?sort=my_score:desc", headers=headers)
    numbers = [item["movie_number"] for item in library.json()["items"]]
    assert numbers[:2] == ["RT-101", "RT-102"]
    assert numbers[-1] == "RT-103"
    assert all(item["my_rating_values"] == {} for item in library.json()["items"])

    bad_sort = client.get("/movies?sort=dimension_999999:desc", headers=headers)
    assert bad_sort.status_code == 422
    assert bad_sort.json()["error"]["code"] == "invalid_movie_filter"

    assert RatingDimension.select().count() == 2


def test_rated_movie_list_combines_status_and_collection_type_filters(
    client, account_user
):
    headers = _auth_headers(client, account_user)
    dimension = _create_dimension(client, headers, "筛选评分")
    library = MediaLibrary.create(
        name="rating-filter-library",
        provider_key="test",
        provider_config={},
    )

    subscribed_single = _create_movie(
        "RT-FILTER-SUB", is_subscribed=True, is_collection=False
    )
    unsubscribed_single = _create_movie(
        "RT-FILTER-UNSUB", is_subscribed=False, is_collection=False
    )
    playable_single = _create_movie(
        "RT-FILTER-PLAY", is_subscribed=False, is_collection=False
    )
    collection = _create_movie(
        "RT-FILTER-COLLECTION", is_subscribed=True, is_collection=True
    )
    Media.create(
        movie=playable_single,
        library=library,
        file_name="playable.mp4",
        valid=True,
    )

    for movie in (
        subscribed_single,
        unsubscribed_single,
        playable_single,
        collection,
    ):
        assert (
            _save_rating(client, headers, movie.movie_number, {dimension["id"]: 4})
            .status_code
            == 200
        )

    subscribed = client.get(
        "/movies",
        headers=headers,
        params={
            "rated": True,
            "status": "subscribed",
            "collection_type": "single",
        },
    )
    assert subscribed.status_code == 200
    assert [item["movie_number"] for item in subscribed.json()["items"]] == [
        "RT-FILTER-SUB"
    ]

    unsubscribed = client.get(
        "/movies",
        headers=headers,
        params={
            "rated": True,
            "status": "unsubscribed",
            "collection_type": "single",
        },
    )
    assert [item["movie_number"] for item in unsubscribed.json()["items"]] == [
        "RT-FILTER-PLAY",
        "RT-FILTER-UNSUB",
    ]

    playable = client.get(
        "/movies",
        headers=headers,
        params={"rated": True, "status": "playable", "collection_type": "all"},
    )
    assert [item["movie_number"] for item in playable.json()["items"]] == [
        "RT-FILTER-PLAY"
    ]

    all_single = client.get(
        "/movies",
        headers=headers,
        params={
            "rated": True,
            "status": "all",
            "collection_type": "single",
        },
    )
    assert all_single.json()["total"] == 3
