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
        name="三上悠亞",
        alias_name="鬼头桃菜",
        gender=1,
        height_cm=160,
    )
    simplified = Actor.create(
        name="三上悠亚", gender=1, height_cm=170
    )
    exact = Actor.create(name="三上", gender=1)
    alias_only = Actor.create(
        name="別名俳優", alias_name="鬼头桃菜", gender=1
    )
    unrelated = Actor.create(name="無関係", gender=1)
    male = Actor.create(name="三上男", gender=2)
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


def test_search_actors_matches_display_name_override(
    client, account_user, test_db
):
    exact = Actor.create(
        name="来源名称",
        display_name_override="本地显示名",
        gender=1,
    )
    prefix = Actor.create(
        name="另一个来源",
        display_name_override="本地显示名字",
        gender=1,
    )
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="本地显示名")
    assert _item_ids(body) == [exact.id, prefix.id]
    assert body["items"][0]["display_name"] == "本地显示名"

    source_name_body = _search(client, headers, query="来源名称")
    assert _item_ids(source_name_body) == [exact.id]


def test_search_actors_matches_simplified_japanese_and_traditional_forms(
    client, account_user, test_db
):
    simplified = Actor.create(name="亚美", gender=1)
    japanese = Actor.create(name="亜美", gender=1)
    traditional = Actor.create(name="亞美", gender=1)
    alias_only = Actor.create(name="别名演员", alias_name="亚美", gender=1)
    display_override = Actor.create(
        name="显示名演员",
        display_name_override="亜美",
        gender=1,
    )
    headers = _login(client, account_user.username)
    expected_ids = {
        simplified.id,
        japanese.id,
        traditional.id,
        alias_only.id,
        display_override.id,
    }

    for query in ("亚美", "亜美", "亞美"):
        body = _search(client, headers, query=query)
        assert set(_item_ids(body)) == expected_ids

    assert _item_ids(_search(client, headers, query="亚美"))[0] == simplified.id
    assert _item_ids(_search(client, headers, query="亜美"))[0] == japanese.id
    assert _item_ids(_search(client, headers, query="亞美"))[0] == traditional.id


def test_search_actors_han_variants_keep_multi_word_and_semantics(
    client, account_user, test_db
):
    matching = Actor.create(name="亜美 東京", gender=1)
    Actor.create(name="大阪演员", gender=1)
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="亚美 东京")

    assert _item_ids(body) == [matching.id]


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
