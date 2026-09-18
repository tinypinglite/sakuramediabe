"""本地目录搜索：影片列表 query 参数的行为回归。

覆盖番号片段、片名、演员名、标签、多词 AND、相关度排序、转义、筛选组合与分页。
"""

from datetime import datetime

import pytest

from src.model import Actor, Media, MediaLibrary, Movie, MovieActor, MovieTag, Tag


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(
    movie_number: str, title: str, *, release_date: datetime | None = None
) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=title,
        release_date=release_date,
    )


def _create_media(movie: Movie) -> Media:
    library, _ = MediaLibrary.get_or_create(
        name="search-test-library",
        defaults={"provider_key": "test", "provider_config": {}},
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name=f"{movie.movie_number}.mp4",
        valid=True,
    )


def _search(client, headers, **params) -> dict:
    response = client.get("/movies", headers=headers, params=params)
    assert response.status_code == 200
    return response.json()


@pytest.fixture()
def search_movies(test_db):
    actor = Actor.create(
        javdb_id="actor-sanshang", name="三上悠亞", alias_name="鬼头桃菜"
    )
    tag = Tag.create(name="痴女")
    onsen = _create_movie(
        "SSNI-888", "新人 温泉旅行 完全版", release_date=datetime(2023, 5, 1)
    )
    kiss = _create_movie("SSNI-889", "三上悠亞 接吻", release_date=datetime(2024, 2, 1))
    fc2 = _create_movie("FC2-PPV-1234567", "温泉 4時間", release_date=datetime(2022, 1, 1))
    tagged = _create_movie("ABP-001", "無関係な作品")
    MovieActor.create(movie=kiss, actor=actor)
    MovieActor.create(movie=fc2, actor=actor)
    MovieTag.create(movie=tagged, tag=tag)
    MovieTag.create(movie=fc2, tag=tag)
    return onsen, kiss, fc2, tagged, actor, tag


def _item_ids(body: dict) -> list[int]:
    return [item["id"] for item in body["items"]]


def test_search_matches_number_fragments(client, account_user, search_movies):
    onsen, _kiss, _fc2, _tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    assert _item_ids(_search(client, headers, query="SSNI888")) == [onsen.id]
    assert _item_ids(_search(client, headers, query="SSNI-888")) == [onsen.id]
    assert _item_ids(_search(client, headers, query="ssni_888")) == [onsen.id]


def test_search_number_exact_ranks_before_title_containing_number(
    client, account_user, search_movies
):
    onsen, _kiss, _fc2, _tagged, _actor, _tag = search_movies
    title_match = _create_movie("XYZ-001", "SSNI-888 特集")
    headers = _login(client, account_user.username)

    assert _item_ids(_search(client, headers, query="SSNI-888")) == [
        onsen.id,
        title_match.id,
    ]


def test_search_matches_actor_name_and_alias(client, account_user, search_movies):
    _onsen, kiss, fc2, _tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    name_body = _search(client, headers, query="三上")
    assert set(_item_ids(name_body)) == {kiss.id, fc2.id}
    # 片名命中优先于仅演员命中；同一影片被片名和演员同时命中时不重复。
    assert _item_ids(name_body)[0] == kiss.id

    assert set(_item_ids(_search(client, headers, query="鬼头桃菜"))) == {
        kiss.id,
        fc2.id,
    }


def test_search_matches_tag_name(client, account_user, search_movies):
    _onsen, _kiss, fc2, tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    assert set(_item_ids(_search(client, headers, query="痴女"))) == {
        tagged.id,
        fc2.id,
    }


def test_search_multi_terms_require_all_terms(client, account_user, search_movies):
    _onsen, _kiss, fc2, _tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    # 温泉命中片名、三上命中演员：只有同时满足两个词的 FC2 影片命中。
    assert _item_ids(_search(client, headers, query="温泉 三上")) == [fc2.id]
    assert _item_ids(_search(client, headers, query="温泉 不存在")) == []


def test_search_escapes_like_wildcards(client, account_user, search_movies):
    headers = _login(client, account_user.username)

    assert _search(client, headers, query="%")["total"] == 0
    assert _search(client, headers, query="_")["total"] == 0


def test_search_combines_with_existing_filters(client, account_user, search_movies):
    onsen, _kiss, _fc2, _tagged, _actor, _tag = search_movies
    _create_media(onsen)
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="温泉", status="playable")
    assert _item_ids(body) == [onsen.id]


def test_search_paginates_with_exact_total(client, account_user, search_movies):
    _onsen, kiss, _fc2, _tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    first_page = _search(client, headers, query="SSNI", page=1, page_size=1)
    second_page = _search(client, headers, query="SSNI", page=2, page_size=1)
    assert first_page["total"] == 2
    assert _item_ids(first_page) == [kiss.id]
    assert _item_ids(second_page) != _item_ids(first_page)


def test_search_explicit_sort_overrides_relevance(client, account_user, search_movies):
    onsen, _kiss, fc2, _tagged, _actor, _tag = search_movies
    headers = _login(client, account_user.username)

    body = _search(client, headers, query="温泉", sort="release_date:asc")
    assert _item_ids(body) == [fc2.id, onsen.id]


def test_search_matches_lowercase_canonical_number(client, account_user, search_movies):
    # 东热等番号的规范写法是小写，匹配必须大小写不敏感。
    n0646 = _create_movie("n0646", "東熱作品")
    headers = _login(client, account_user.username)

    assert _item_ids(_search(client, headers, query="N0646")) == [n0646.id]
    assert _item_ids(_search(client, headers, query="n0646")) == [n0646.id]
    assert _item_ids(_search(client, headers, query="0646")) == [n0646.id]


def test_search_preserves_pure_digit_separator_semantics(
    client, account_user, search_movies
):
    underscore = _create_movie("222333_444", "一本道作品")
    hyphen = _create_movie("222333-444", "加勒比作品")
    headers = _login(client, account_user.username)

    assert _item_ids(_search(client, headers, query="222333_444")) == [underscore.id]
    assert _item_ids(_search(client, headers, query="222333-444")) == [hyphen.id]
    assert set(_item_ids(_search(client, headers, query="222333"))) == {
        underscore.id,
        hyphen.id,
    }


def test_search_matches_fc2_number_forms(client, account_user, search_movies):
    _onsen, _kiss, fc2, _tagged, _actor, _tag = search_movies
    canonical = _create_movie("FC2-7654321", "FC2 テスト")
    headers = _login(client, account_user.username)

    # 存储形态为 FC2-PPV-1234567 时，PPV 与非 PPV 输入都能命中。
    assert _item_ids(_search(client, headers, query="FC2-PPV-1234567")) == [fc2.id]
    assert _item_ids(_search(client, headers, query="FC2-1234567")) == [fc2.id]
    # 存储形态为 FC2-7654321 时同样兼容两种输入。
    assert _item_ids(_search(client, headers, query="FC2-7654321")) == [canonical.id]
    assert _item_ids(_search(client, headers, query="FC2-PPV-7654321")) == [
        canonical.id
    ]


def test_search_without_query_keeps_list_behavior(client, account_user, search_movies):
    headers = _login(client, account_user.username)

    body = _search(client, headers)
    assert body["total"] == 4
    assert _search(client, headers, query="")["total"] == 4
    assert _search(client, headers, query="   ")["total"] == 4


def test_search_rejects_invalid_query_length(client, account_user, search_movies):
    headers = _login(client, account_user.username)

    too_many_terms = client.get(
        "/movies", headers=headers, params={"query": "一 二 三 四 五 六 七"}
    )
    assert too_many_terms.status_code == 422
    assert too_many_terms.json()["error"]["code"] == "invalid_movie_filter"

    overlong_term = client.get(
        "/movies", headers=headers, params={"query": "a" * 65}
    )
    assert overlong_term.status_code == 422
    assert overlong_term.json()["error"]["code"] == "invalid_movie_filter"
