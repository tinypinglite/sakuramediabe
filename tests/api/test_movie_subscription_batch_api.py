"""影片批量订阅 / 取消订阅端点的部分成功语义回归测试。

历史 bug：`batch_unsubscribe_movies` 里 has_media 判定用 `movie.id`（int）作
`Media.movie.in_(...)` 的参数，但 `Media.movie` 外键 `field=Movie.movie_number`
（字符串列），生成 SQL 变成 `WHERE movie_number IN (1,2,3)` 恒不命中，
导致所有走批量端点的影片都被判为"无 media"、直接取消订阅成功。这里的对齐单条
`unsubscribe_movie` 的两个用例专门守卫这条链路。
"""

from src.model import Media, MediaLibrary, Movie

SUBSCRIPTIONS_PATH = "/movies/subscriptions"
UNSUBSCRIPTIONS_PATH = "/movies/unsubscriptions"


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, *, is_subscribed: bool = True) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        is_subscribed=is_subscribed,
    )


def _create_media(movie: Movie) -> Media:
    library, _ = MediaLibrary.get_or_create(
        name="test-library",
        defaults={"provider_key": "test", "provider_config": {}},
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name=f"{movie.movie_number}.mp4",
        valid=True,
    )


def test_batch_unsubscribe_skips_movies_with_media(client, account_user):
    """核心回归：批量取消订阅时存在 provider media 的影片必须进 skipped[has_media]，
    与单条 unsubscribe_movie 抛 409 movie_subscription_has_media 语义一致。"""
    token = _login(client, account_user.username)
    with_media = _create_movie("ABC-001")
    _create_media(with_media)
    clean = _create_movie("ABC-002")

    response = client.post(
        UNSUBSCRIPTIONS_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": ["ABC-001", "ABC-002"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_count"] == 2
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 1
    assert body["skipped"] == [
        {"movie_number": "ABC-001", "reason": "has_media"},
    ]

    # 有 media 的影片保持已订阅；无 media 的被正常取消。
    with_media_fresh = Movie.get(Movie.id == with_media.id)
    clean_fresh = Movie.get(Movie.id == clean.id)
    assert with_media_fresh.is_subscribed is True
    assert clean_fresh.is_subscribed is False
    assert clean_fresh.subscribed_at is None


def test_batch_unsubscribe_reports_movie_not_found_alongside_has_media(
    client, account_user
):
    token = _login(client, account_user.username)
    with_media = _create_movie("ABP-100")
    _create_media(with_media)

    response = client.post(
        UNSUBSCRIPTIONS_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": ["ABP-100", "NOT-EXIST-999"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_count"] == 2
    assert body["updated_count"] == 0
    assert body["skipped_count"] == 2
    # 顺序：先 movie_not_found（先按 ordered_numbers 收集），再 has_media（后追加）
    assert body["skipped"] == [
        {"movie_number": "NOT-EXIST-999", "reason": "movie_not_found"},
        {"movie_number": "ABP-100", "reason": "has_media"},
    ]

    with_media_fresh = Movie.get(Movie.id == with_media.id)
    assert with_media_fresh.is_subscribed is True


def test_batch_unsubscribe_updates_movies_without_media(client, account_user):
    token = _login(client, account_user.username)
    first = _create_movie("ABC-010")
    second = _create_movie("ABC-011")

    response = client.post(
        UNSUBSCRIPTIONS_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": ["ABC-010", "ABC-011"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_count"] == 2
    assert body["updated_count"] == 2
    assert body["skipped_count"] == 0
    assert body["skipped"] == []
    for movie in (first, second):
        fresh = Movie.get(Movie.id == movie.id)
        assert fresh.is_subscribed is False
        assert fresh.subscribed_at is None


def test_batch_subscribe_marks_matched_movies_and_reports_unknown_numbers(
    client, account_user
):
    token = _login(client, account_user.username)
    existing = _create_movie("ABC-020", is_subscribed=False)

    response = client.post(
        SUBSCRIPTIONS_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": ["ABC-020", "NOT-EXIST-999"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_count"] == 2
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 1
    assert body["skipped"] == [
        {"movie_number": "NOT-EXIST-999", "reason": "movie_not_found"},
    ]

    fresh = Movie.get(Movie.id == existing.id)
    assert fresh.is_subscribed is True
    assert fresh.subscribed_at is not None
