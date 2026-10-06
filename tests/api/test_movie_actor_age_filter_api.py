"""影片列表出演年龄筛选：按最老女优在影片发行日的周岁区间过滤。"""

from datetime import date, datetime

from src.model import Actor, Movie, MovieActor


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(number: str, release_date: datetime | None) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{number}",
        movie_number=number,
        title=number,
        release_date=release_date,
    )


def _create_actor(
    javdb_id: str,
    birthday: date | None,
    gender: int = 1,
) -> Actor:
    return Actor.create(
        javdb_id=javdb_id,
        name=javdb_id,
        gender=gender,
        birthday=birthday,
    )


def _link(movie: Movie, actor: Actor) -> None:
    MovieActor.create(movie=movie, actor=actor)


def _item_numbers(body: dict) -> set[str]:
    return {item["movie_number"] for item in body["items"]}


def test_movie_actor_age_filter_uses_age_at_release_date(client, account_user):
    headers = _login(client, account_user.username)
    actress = _create_actor("age-at-release", date(2000, 1, 1))
    early = _create_movie("AGE-EARLY", datetime(2019, 6, 1))
    late = _create_movie("AGE-LATE", datetime(2025, 1, 1))
    _link(early, actress)
    _link(late, actress)

    no_filter = client.get("/movies", headers=headers)
    assert no_filter.json()["total"] == 2

    older = client.get("/movies", headers=headers, params={"actor_age_min": 24})
    assert older.status_code == 200
    assert older.json()["total"] == 1
    assert _item_numbers(older.json()) == {"AGE-LATE"}

    younger = client.get("/movies", headers=headers, params={"actor_age_max": 20})
    assert younger.status_code == 200
    assert younger.json()["total"] == 1
    assert _item_numbers(younger.json()) == {"AGE-EARLY"}

    both = client.get(
        "/movies",
        headers=headers,
        params={"actor_age_min": 18, "actor_age_max": 20},
    )
    assert both.json()["total"] == 1
    assert _item_numbers(both.json()) == {"AGE-EARLY"}


def test_movie_actor_age_filter_uses_oldest_actress_at_release(client, account_user):
    headers = _login(client, account_user.username)
    release = datetime(2020, 1, 1)
    young = _create_actor("age-young", date(1998, 1, 1))
    mature = _create_actor("age-mature", date(1975, 1, 1))
    mixed = _create_movie("AGE-MIXED", release)
    _link(mixed, young)
    _link(mixed, mature)
    single = _create_movie("AGE-SINGLE", release)
    _link(single, young)

    up_to_30 = client.get("/movies", headers=headers, params={"actor_age_max": 30})
    assert up_to_30.status_code == 200
    assert up_to_30.json()["total"] == 1
    assert _item_numbers(up_to_30.json()) == {"AGE-SINGLE"}

    from_40 = client.get("/movies", headers=headers, params={"actor_age_min": 40})
    assert from_40.status_code == 200
    assert from_40.json()["total"] == 1
    assert _item_numbers(from_40.json()) == {"AGE-MIXED"}

    range_18_30 = client.get(
        "/movies",
        headers=headers,
        params={"actor_age_min": 18, "actor_age_max": 30},
    )
    assert range_18_30.status_code == 200
    assert range_18_30.json()["total"] == 1
    assert _item_numbers(range_18_30.json()) == {"AGE-SINGLE"}


def test_movie_actor_age_filter_boundary_on_release_anniversary(client, account_user):
    headers = _login(client, account_user.username)
    actress = _create_actor("age-boundary", date(2000, 6, 15))
    exact = _create_movie("AGE-EXACT-18", datetime(2018, 6, 15))
    almost = _create_movie("AGE-ALMOST-18", datetime(2018, 6, 14))
    _link(exact, actress)
    _link(almost, actress)

    at_least_18 = client.get("/movies", headers=headers, params={"actor_age_min": 18})
    assert at_least_18.status_code == 200
    assert at_least_18.json()["total"] == 1
    assert _item_numbers(at_least_18.json()) == {"AGE-EXACT-18"}

    at_most_17 = client.get("/movies", headers=headers, params={"actor_age_max": 17})
    assert at_most_17.status_code == 200
    assert at_most_17.json()["total"] == 1
    assert _item_numbers(at_most_17.json()) == {"AGE-ALMOST-18"}


def test_movie_actor_age_filter_leap_day_boundary(client, account_user):
    headers = _login(client, account_user.username)
    actress = _create_actor("age-leap", date(2000, 2, 29))
    eve = _create_movie("AGE-LEAP-EVE", datetime(2018, 2, 28))
    after = _create_movie("AGE-LEAP-AFTER", datetime(2018, 3, 1))
    _link(eve, actress)
    _link(after, actress)

    # 2/29 出生者按元组口径：2018-02-28 时未满 18（17 岁），2018-03-01 起满 18。
    at_least_18 = client.get("/movies", headers=headers, params={"actor_age_min": 18})
    assert at_least_18.status_code == 200
    assert _item_numbers(at_least_18.json()) == {"AGE-LEAP-AFTER"}

    at_most_17 = client.get("/movies", headers=headers, params={"actor_age_max": 17})
    assert at_most_17.status_code == 200
    assert _item_numbers(at_most_17.json()) == {"AGE-LEAP-EVE"}


def test_movie_actor_age_filter_supports_single_age_window(client, account_user):
    headers = _login(client, account_user.username)
    release = datetime(2020, 1, 1)
    exact = _create_actor("age-window-22", date(1998, 1, 1))
    younger = _create_actor("age-window-19", date(2001, 1, 1))
    window_exact = _create_movie("AGE-WINDOW-EXACT", release)
    _link(window_exact, exact)
    window_younger = _create_movie("AGE-WINDOW-YOUNGER", release)
    _link(window_younger, younger)

    response = client.get(
        "/movies",
        headers=headers,
        params={"actor_age_min": 22, "actor_age_max": 22},
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert _item_numbers(response.json()) == {"AGE-WINDOW-EXACT"}


def test_movie_actor_age_filter_ignores_actresses_without_birthday_in_mix(
    client, account_user
):
    headers = _login(client, account_user.username)
    release = datetime(2020, 1, 1)
    known = _create_actor("age-mix-known", date(1998, 1, 1))
    unknown = _create_actor("age-mix-unknown", None)
    movie = _create_movie("AGE-MIXED-BIRTHDAY", release)
    _link(movie, known)
    _link(movie, unknown)

    # 无生日女优不参与「最老」计算，影片按有生日者的 22 岁参与筛选。
    response = client.get("/movies", headers=headers, params={"actor_age_max": 25})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert _item_numbers(response.json()) == {"AGE-MIXED-BIRTHDAY"}


def test_movie_actor_age_filter_excludes_unknown_data(client, account_user):
    headers = _login(client, account_user.username)
    release = datetime(2020, 1, 1)
    known = _create_actor("age-known", date(1998, 1, 1))
    unknown = _create_actor("age-unknown", None)
    male = _create_actor("age-male", date(1998, 1, 1), gender=2)

    valid = _create_movie("AGE-VALID", release)
    _link(valid, known)
    no_birthday = _create_movie("AGE-NO-BIRTHDAY", release)
    _link(no_birthday, unknown)
    _create_movie("AGE-NO-ACTOR", release)
    male_only = _create_movie("AGE-MALE-ONLY", release)
    _link(male_only, male)
    no_release = _create_movie("AGE-NO-RELEASE", None)
    _link(no_release, known)

    response = client.get("/movies", headers=headers, params={"actor_age_max": 30})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert _item_numbers(response.json()) == {"AGE-VALID"}


def test_movie_actor_age_filter_rejects_min_greater_than_max(client, account_user):
    headers = _login(client, account_user.username)
    response = client.get(
        "/movies",
        headers=headers,
        params={"actor_age_min": 40, "actor_age_max": 20},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_movie_actor_age_filter_rejects_out_of_range_values(client, account_user):
    headers = _login(client, account_user.username)
    for params in (
        {"actor_age_min": 201},
        {"actor_age_max": 201},
        {"actor_age_min": 9999},
        {"actor_age_max": 9999},
    ):
        response = client.get("/movies", headers=headers, params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_movie_filter"
