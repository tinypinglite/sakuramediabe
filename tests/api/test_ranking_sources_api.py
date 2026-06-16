from src.common.runtime_time import runtime_now
from src.model import Image, Media, Movie, RankingItem


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


def test_ranking_sources_router_requires_authentication(client):
    sources_response = client.get("/ranking-sources")
    boards_response = client.get("/ranking-sources/javdb/boards")
    items_response = client.get("/ranking-sources/javdb/boards/censored/items?period=daily")

    assert sources_response.status_code == 401
    assert boards_response.status_code == 401
    assert items_response.status_code == 401


def test_list_ranking_sources_and_boards(client, account_user):
    token = _login(client, username=account_user.username)

    sources_response = client.get(
        "/ranking-sources",
        headers={"Authorization": f"Bearer {token}"},
    )
    boards_response = client.get(
        "/ranking-sources/javdb/boards",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert sources_response.status_code == 200
    assert sources_response.json() == [
        {
            "source_key": "javdb",
            "name": "JavDB",
        },
        {
            "source_key": "missav",
            "name": "MissAV",
        }
    ]

    assert boards_response.status_code == 200
    boards = boards_response.json()
    # board 顺序固定：播放榜在前、常规榜居中、top250 最后。
    assert [b["board_key"] for b in boards] == [
        "playback_all",
        "playback_high_score",
        "censored",
        "uncensored",
        "fc2",
        "top250",
    ]
    # 非 top250 的 board 周期是静态的，逐一精确校验。
    non_top250 = {b["board_key"]: b for b in boards if b["board_key"] != "top250"}
    assert non_top250["playback_all"] == {
        "source_key": "javdb",
        "board_key": "playback_all",
        "name": "热播",
        "supported_periods": ["daily", "weekly", "monthly"],
        "default_period": "daily",
    }
    assert non_top250["playback_high_score"]["name"] == "高评分"
    assert non_top250["censored"]["name"] == "有码"
    assert non_top250["uncensored"]["name"] == "无码"
    assert non_top250["fc2"]["name"] == "FC2"
    for board_key in ("playback_high_score", "censored", "uncensored", "fc2"):
        assert non_top250[board_key]["supported_periods"] == ["daily", "weekly", "monthly"]
    # top250 的年份维度动态滚动，只校验固定子榜在前、含当前年、默认 all。
    top250 = next(b for b in boards if b["board_key"] == "top250")
    assert top250["name"] == "TOP250"
    assert top250["default_period"] == "all"
    assert top250["supported_periods"][:4] == ["all", "uncensored", "censored", "fc2"]
    assert str(runtime_now().year) in top250["supported_periods"]

    missav_boards_response = client.get(
        "/ranking-sources/missav/boards",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert missav_boards_response.status_code == 200
    assert missav_boards_response.json() == [
        {
            "source_key": "missav",
            "board_key": "all",
            "name": "综合",
            "supported_periods": ["daily", "weekly", "monthly"],
            "default_period": "daily",
        }
    ]


def test_list_ranking_board_items_returns_ranked_movie_list(client, account_user):
    token = _login(client, username=account_user.username)
    thin_cover_image = Image.create(
        origin="ranking/thin-origin.jpg",
        small="ranking/thin-small.jpg",
        medium="ranking/thin-medium.jpg",
        large="ranking/thin-large.jpg",
    )
    movie_a = _create_movie(
        "ABP-001",
        "MovieA1",
        title="Movie A",
        title_zh="榜单中文 A",
        thin_cover_image=thin_cover_image,
    )
    movie_b = _create_movie("ABP-002", "MovieA2", title="Movie B")
    movie_c = _create_movie("ABP-003", "MovieA3", title="Movie C")
    Media.create(movie=movie_b, path="/library/main/abp-002.mp4", valid=True)

    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=2,
        movie_number=movie_b.movie_number,
        movie=movie_b,
    )
    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=1,
        movie_number=movie_a.movie_number,
        movie=movie_a,
    )
    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=3,
        movie_number=movie_c.movie_number,
        movie=movie_c,
    )

    response = client.get(
        "/ranking-sources/javdb/boards/censored/items?period=daily&page=1&page_size=2",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert payload["total"] == 3
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert [item["movie_number"] for item in payload["items"]] == ["ABP-001", "ABP-002"]
    assert payload["items"][0]["can_play"] is False
    assert payload["items"][1]["can_play"] is True
    assert payload["items"][0]["title_zh"] == "榜单中文 A"
    assert payload["items"][0]["thin_cover_image"]["id"] == thin_cover_image.id
    assert payload["items"][1]["thin_cover_image"] is None
    assert payload["items"][0]["is_collection"] is False
    assert payload["items"][0]["is_subscribed"] is False
    # 响应顶层暴露该榜单+周期的抓取时间
    assert payload["synced_at"] is not None


def test_list_ranking_board_items_validates_source_board_and_period(client, account_user):
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    invalid_source = client.get(
        "/ranking-sources/foobar/boards/censored/items?period=daily",
        headers=headers,
    )
    invalid_board = client.get(
        "/ranking-sources/javdb/boards/newcomer/items?period=daily",
        headers=headers,
    )
    missing_period = client.get(
        "/ranking-sources/javdb/boards/censored/items",
        headers=headers,
    )
    unsupported_period = client.get(
        "/ranking-sources/javdb/boards/censored/items?period=yearly",
        headers=headers,
    )

    assert invalid_source.status_code == 404
    assert invalid_source.json()["error"]["code"] == "ranking_source_not_found"
    assert invalid_board.status_code == 404
    assert invalid_board.json()["error"]["code"] == "ranking_board_not_found"
    assert missing_period.status_code == 422
    assert missing_period.json()["error"]["code"] == "invalid_ranking_period"
    assert unsupported_period.status_code == 422
    assert unsupported_period.json()["error"]["code"] == "invalid_ranking_period"


def test_list_missav_ranking_board_items_returns_ranked_movie_list(client, account_user):
    token = _login(client, username=account_user.username)
    movie_a = _create_movie("ABP-001", "MovieA1", title="Movie A")
    movie_b = _create_movie("ABP-002", "MovieA2", title="Movie B")
    Media.create(movie=movie_b, path="/library/main/abp-002.mp4", valid=True)

    RankingItem.create(
        source_key="missav",
        board_key="all",
        period="daily",
        rank=2,
        movie_number=movie_b.movie_number,
        movie=movie_b,
    )
    RankingItem.create(
        source_key="missav",
        board_key="all",
        period="daily",
        rank=1,
        movie_number=movie_a.movie_number,
        movie=movie_a,
    )

    response = client.get(
        "/ranking-sources/missav/boards/all/items?period=daily",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["rank"] for item in payload["items"]] == [1, 2]
    assert [item["movie_number"] for item in payload["items"]] == ["ABP-001", "ABP-002"]
    assert payload["items"][0]["can_play"] is False
    assert payload["items"][1]["can_play"] is True
