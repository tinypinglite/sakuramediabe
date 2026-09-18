"""本地目录搜索：演员列表 query 参数的行为回归。"""

import pytest

from src.model import Actor


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _search(client, headers, **params) -> dict:
    response = client.get("/actors", headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


def _item_ids(body: dict) -> list[int]:
    return [item["id"] for item in body["items"]]


@pytest.fixture()
def search_actors(test_db):
    sanshang = Actor.create(
        javdb_id="actor-sanshang",
        name="三上悠亞",
        alias_name="鬼头桃菜",
        gender=1,
        height_cm=160,
    )
    simplified = Actor.create(
        javdb_id="actor-simplified", name="三上悠亚", gender=1, height_cm=170
    )
    exact = Actor.create(javdb_id="actor-exact", name="三上", gender=1)
    alias_only = Actor.create(
        javdb_id="actor-alias", name="別名俳優", alias_name="鬼头桃菜", gender=1
    )
    unrelated = Actor.create(javdb_id="actor-unrelated", name="無関係", gender=1)
    male = Actor.create(javdb_id="actor-male", name="三上男", gender=2)
    return sanshang, simplified, exact, alias_only, unrelated, male


def test_search_actors_ranks_exact_name_first(client, account_user, search_actors):
    sanshang, simplified, exact, _alias_only, _unrelated, male = search_actors
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="三上")
    assert _item_ids(body) == [exact.id, sanshang.id, simplified.id, male.id]


def test_search_actors_matches_alias(client, account_user, search_actors):
    sanshang, _simplified, _exact, alias_only, _unrelated, _male = search_actors
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="鬼头")
    assert set(_item_ids(body)) == {sanshang.id, alias_only.id}


def test_search_actors_combines_with_existing_filters(
    client, account_user, search_actors
):
    _sanshang, _simplified, _exact, _alias_only, _unrelated, male = search_actors
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="三上", gender="female")
    assert body["total"] == 3
    assert male.id not in _item_ids(body)


def test_search_actors_explicit_sort_overrides_relevance(
    client, account_user, search_actors
):
    sanshang, simplified, _exact, _alias_only, _unrelated, _male = search_actors
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="三上", sort="height_cm:desc")
    assert _item_ids(body)[:2] == [simplified.id, sanshang.id]


def test_search_actors_paginates_with_exact_total(client, account_user, search_actors):
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="三上", page=1, page_size=2)
    assert body["total"] == 4
    assert len(body["items"]) == 2


def test_search_actors_without_query_keeps_list_behavior(
    client, account_user, search_actors
):
    headers = _login(client, account_user.username)

    body = _search(client, headers)
    assert body["total"] == 6


def test_actor_search_rejects_invalid_query(client, account_user, search_actors):
    headers = _login(client, account_user.username)

    response = client.get(
        "/actors", headers=headers, params={"query": "一 二 三 四 五 六 七"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_actor_filter"
