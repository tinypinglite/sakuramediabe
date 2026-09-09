from datetime import date, datetime

import pytest

from src.model import Actor


def _headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def actors(test_db, monkeypatch):
    monkeypatch.setattr(
        "src.service.catalog.actor_service.utc_now_for_db",
        lambda: datetime(2026, 9, 10),
    )
    young = Actor.create(
        javdb_id="young",
        name="年轻",
        gender=1,
        is_subscribed=True,
        birthday=date(2004, 9, 11),
        height_cm=160,
        bust_cm=82,
        waist_cm=60,
        hips_cm=90,
        cup="B",
    )
    older = Actor.create(
        javdb_id="older",
        name="年长",
        gender=1,
        is_subscribed=True,
        birthday=date(1996, 9, 10),
        height_cm=170,
        bust_cm=90,
        waist_cm=66,
        hips_cm=90,
        cup="C",
    )
    unknown = Actor.create(
        javdb_id="unknown",
        name="资料缺失",
        gender=1,
        is_subscribed=True,
        height_cm=161,
        cup=" b ",
    )
    Actor.create(
        javdb_id="male",
        name="男优",
        gender=2,
        is_subscribed=True,
        birthday=date(2004, 9, 11),
        height_cm=160,
        cup="B",
    )
    return young, older, unknown


def test_list_actors_filters_profiles_and_sorts_nullable_values(
    client, account_user, actors
):
    young, older, unknown = actors
    headers = _headers(client, account_user)

    response = client.get(
        "/actors",
        headers=headers,
        params={
            "gender": "female",
            "subscription_status": "subscribed",
            "age_min": 21,
            "age_max": 21,
            "height_min": 155,
            "height_max": 165,
            "cups": "B",
        },
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [young.id]

    ratio_response = client.get(
        "/actors",
        headers=headers,
        params={
            "gender": "female",
            "subscription_status": "subscribed",
            "sort": "waist_hip_ratio:asc",
        },
    )
    assert ratio_response.status_code == 200
    assert [item["id"] for item in ratio_response.json()["items"]] == [
        young.id,
        older.id,
        unknown.id,
    ]

    age_response = client.get(
        "/actors",
        headers=headers,
        params={
            "gender": "female",
            "subscription_status": "subscribed",
            "sort": "age:asc",
        },
    )
    assert age_response.status_code == 200
    assert [item["id"] for item in age_response.json()["items"]] == [
        young.id,
        older.id,
        unknown.id,
    ]

    cup_response = client.get(
        "/actors",
        headers=headers,
        params={
            "gender": "female",
            "subscription_status": "subscribed",
            "sort": "cup:desc",
        },
    )
    assert cup_response.status_code == 200
    assert [item["id"] for item in cup_response.json()["items"]] == [
        older.id,
        unknown.id,
        young.id,
    ]


def test_actor_filter_options_follow_scope_and_report_populated_values(
    client, account_user, actors
):
    headers = _headers(client, account_user)

    response = client.get(
        "/actors/filter-options",
        headers=headers,
        params={"gender": "female", "subscription_status": "subscribed"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "actor_count": 3,
        "as_of_date": "2026-09-10",
        "age": {"min": 21, "max": 30, "populated_count": 2},
        "height_cm": {"min": 160, "max": 170, "populated_count": 3},
        "cups": [
            {"value": "B", "count": 2},
            {"value": "C", "count": 1},
        ],
    }


def test_actor_profile_filters_allow_unscoped_queries(client, account_user, actors):
    headers = _headers(client, account_user)

    listing = client.get("/actors", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 4

    options = client.get("/actors/filter-options", headers=headers)
    assert options.status_code == 200
    assert options.json()["actor_count"] == 4


def test_actor_list_rejects_reversed_ranges(client, account_user, actors):
    response = client.get(
        "/actors",
        headers=_headers(client, account_user),
        params={"age_min": 30, "age_max": 20},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_actor_filter"
