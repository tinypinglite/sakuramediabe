from datetime import datetime

from src.api.exception.errors import ApiError
from src.common.runtime_time import runtime_now
from src.config.config import settings
from src.metadata._providers.exceptions import JavdbAuthError
from src.model import Image, Media, Movie, RankingItem
from src.service.discovery.ranking_service import RankingCatalogService, RankingSyncService
from src.service.system import ActivityService


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


class FakeRankingProvider:
    def __init__(self):
        self.rankings: dict[tuple[str, str], list[str]] = {}
        self.playback: dict[tuple[str, str], list[str]] = {}
        self.top: dict[tuple[str, str], list[str]] = {}
        self.details: dict[str, dict] = {}
        # 记录 top 抓取调用，便于断言历史年份是否被跳过；以及模拟登录失败。
        self.top_calls: list[tuple[str, str]] = []
        self.top_auth_error = False

    def set_ranking(self, video_type: str, period: str, numbers: list[str]) -> None:
        self.rankings[(video_type, period)] = numbers

    def set_playback(self, filter_by: str, period: str, numbers: list[str]) -> None:
        self.playback[(filter_by, period)] = numbers

    def set_top(self, top_type: str, type_value: str, numbers: list[str]) -> None:
        self.top[(top_type, type_value)] = numbers

    def set_detail(self, movie_number: str, javdb_id: str) -> None:
        self.details[movie_number] = {
            "javdb_id": javdb_id,
            "movie_number": movie_number,
            "title": movie_number,
        }

    def get_rank_numbers(self, video_type: str, period: str = "daily") -> list[str]:
        return list(self.rankings.get((video_type, period), []))

    def get_playback_rank_numbers(self, filter_by: str = "all", period: str = "daily") -> list[str]:
        return list(self.playback.get((filter_by, period), []))

    def get_top_numbers(self, top_type: str, type_value: str = "", max_pages=None) -> list[str]:
        self.top_calls.append((top_type, type_value))
        if self.top_auth_error:
            raise JavdbAuthError("fake login failed")
        return list(self.top.get((top_type, type_value), []))

    def get_movie_by_number(self, movie_number: str) -> dict:
        return self.details[movie_number]


class FakeMissavRankingProvider:
    def __init__(self):
        self.rankings: dict[str, list[str]] = {}

    def set_ranking(self, period: str, numbers: list[str]) -> None:
        self.rankings[period] = numbers

    def fetch_rank_numbers(self, period: str) -> list[str]:
        return list(self.rankings.get(period, []))


class FakeCatalogImportService:
    def __init__(self, fail_on: set[str] | None = None):
        self.fail_on = fail_on or set()

    def upsert_movie_from_javdb_detail(self, detail: dict):
        if detail["movie_number"] in self.fail_on:
            raise RuntimeError("import failed")
        movie, _ = Movie.get_or_create(
            javdb_id=detail["javdb_id"],
            defaults={
                "movie_number": detail["movie_number"],
                "title": detail["title"],
            },
        )
        if movie.movie_number != detail["movie_number"] or movie.title != detail["title"]:
            movie.movie_number = detail["movie_number"]
            movie.title = detail["title"]
            movie.save()
        return movie


def test_ranking_sync_service_replaces_scope_items(app):
    provider = FakeRankingProvider()
    provider.set_ranking("0", "daily", ["ABP-001", "ABP-002"])
    provider.set_detail("ABP-001", "MovieA1")
    provider.set_detail("ABP-002", "MovieA2")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": provider},
    )

    first_stats = service.sync_board_period("javdb", "censored", "daily")
    assert first_stats["fetched_numbers"] == 2
    assert first_stats["imported_movies"] == 2
    assert first_stats["stored_items"] == 2
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "javdb",
            RankingItem.board_key == "censored",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-001", "ABP-002"]

    provider.set_ranking("0", "daily", ["ABP-003"])
    provider.set_detail("ABP-003", "MovieA3")
    second_stats = service.sync_board_period("javdb", "censored", "daily")
    assert second_stats["fetched_numbers"] == 1
    assert second_stats["imported_movies"] == 1
    assert second_stats["stored_items"] == 1
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "javdb",
            RankingItem.board_key == "censored",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-003"]


def test_ranking_sync_service_skips_failed_import_items(app):
    provider = FakeRankingProvider()
    provider.set_ranking("0", "daily", ["ABP-001", "ABP-404", "ABP-002"])
    provider.set_detail("ABP-001", "MovieA1")
    provider.set_detail("ABP-404", "MovieA404")
    provider.set_detail("ABP-002", "MovieA2")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(fail_on={"ABP-404"}),
        providers={"javdb": provider},
    )

    stats = service.sync_board_period("javdb", "censored", "daily")

    assert stats["fetched_numbers"] == 3
    assert stats["imported_movies"] == 2
    assert stats["skipped_movies"] == 1
    assert stats["stored_items"] == 2
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "javdb",
            RankingItem.board_key == "censored",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-001", "ABP-002"]


def test_ranking_sync_service_supports_missav_source(app):
    missav_provider = FakeMissavRankingProvider()
    missav_provider.set_ranking("daily", ["ABP-001", "ABP-002"])
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_detail("ABP-001", "MovieA1")
    javdb_provider.set_detail("ABP-002", "MovieA2")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"missav": missav_provider, "javdb": javdb_provider},
    )

    stats = service.sync_board_period("missav", "all", "daily")

    assert stats["fetched_numbers"] == 2
    assert stats["imported_movies"] == 2
    assert stats["skipped_movies"] == 0
    assert stats["stored_items"] == 2
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "missav",
            RankingItem.board_key == "all",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-001", "ABP-002"]


def test_ranking_sync_service_supports_missav_scope_replace(app):
    missav_provider = FakeMissavRankingProvider()
    missav_provider.set_ranking("daily", ["ABP-001", "ABP-002"])
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_detail("ABP-001", "MovieA1")
    javdb_provider.set_detail("ABP-002", "MovieA2")
    javdb_provider.set_detail("ABP-003", "MovieA3")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"missav": missav_provider, "javdb": javdb_provider},
    )

    first_stats = service.sync_board_period("missav", "all", "daily")
    assert first_stats["stored_items"] == 2

    missav_provider.set_ranking("daily", ["ABP-003"])
    second_stats = service.sync_board_period("missav", "all", "daily")

    assert second_stats["stored_items"] == 1
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "missav",
            RankingItem.board_key == "all",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-003"]


def test_ranking_sync_service_skips_failed_import_items_for_missav(app):
    missav_provider = FakeMissavRankingProvider()
    missav_provider.set_ranking("daily", ["ABP-001", "ABP-404", "ABP-002"])
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_detail("ABP-001", "MovieA1")
    javdb_provider.set_detail("ABP-404", "MovieA404")
    javdb_provider.set_detail("ABP-002", "MovieA2")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(fail_on={"ABP-404"}),
        providers={"missav": missav_provider, "javdb": javdb_provider},
    )

    stats = service.sync_board_period("missav", "all", "daily")

    assert stats["fetched_numbers"] == 3
    assert stats["imported_movies"] == 2
    assert stats["skipped_movies"] == 1
    assert stats["stored_items"] == 2
    assert [
        item.movie_number
        for item in RankingItem.select().where(
            RankingItem.source_key == "missav",
            RankingItem.board_key == "all",
            RankingItem.period == "daily",
        ).order_by(RankingItem.rank.asc())
    ] == ["ABP-001", "ABP-002"]


def test_ranking_sync_service_counts_stored_items_when_insert_execute_returns_non_int(
    app,
    monkeypatch,
):
    provider = FakeRankingProvider()
    provider.set_ranking("0", "daily", ["ABP-001", "ABP-002"])
    provider.set_detail("ABP-001", "MovieA1")
    provider.set_detail("ABP-002", "MovieA2")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": provider},
    )

    insert_many = RankingItem.insert_many

    class _NonIntExecuteResult:
        pass

    class _InsertQueryProxy:
        def __init__(self, query):
            self.query = query

        def execute(self):
            self.query.execute()
            return _NonIntExecuteResult()

    def _patched_insert_many(cls, rows):
        return _InsertQueryProxy(insert_many(rows))

    monkeypatch.setattr(RankingItem, "insert_many", classmethod(_patched_insert_many))

    stats = service.sync_board_period("javdb", "censored", "daily")

    assert stats["stored_items"] == 2
    assert (
        RankingItem.select()
        .where(
            RankingItem.source_key == "javdb",
            RankingItem.board_key == "censored",
            RankingItem.period == "daily",
        )
        .count()
        == 2
    )


def test_ranking_catalog_service_lists_ranked_items_in_rank_order(app):
    thin_cover_image = Image.create(
        origin="ranking-service/thin-origin.jpg",
        small="ranking-service/thin-small.jpg",
        medium="ranking-service/thin-medium.jpg",
        large="ranking-service/thin-large.jpg",
    )
    movie_a = _create_movie(
        "ABP-001",
        "MovieA1",
        title_zh="榜单服务中文 A",
        thin_cover_image=thin_cover_image,
    )
    movie_b = _create_movie("ABP-002", "MovieA2")
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

    page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
    )

    assert page.total == 2
    assert [item.rank for item in page.items] == [1, 2]
    assert [item.movie_number for item in page.items] == ["ABP-001", "ABP-002"]
    assert page.items[0].title_zh == "榜单服务中文 A"
    assert page.items[0].thin_cover_image is not None
    assert page.items[0].thin_cover_image.id == thin_cover_image.id
    assert page.items[1].thin_cover_image is None
    assert page.items[0].can_play is False
    assert page.items[1].can_play is True


def test_ranking_catalog_service_exposes_scope_synced_at(app):
    movie_a = _create_movie("ABP-001", "MovieA1")
    movie_b = _create_movie("ABP-002", "MovieA2")
    # 同榜单+周期整批同步，synced_at 取该作用域最新的 updated_at
    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=1,
        movie_number=movie_a.movie_number,
        movie=movie_a,
        updated_at=datetime(2026, 3, 22, 5, 0, 0),
    )
    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=2,
        movie_number=movie_b.movie_number,
        movie=movie_b,
        updated_at=datetime(2026, 3, 22, 6, 30, 0),
    )

    page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
    )

    assert page.model_dump(mode="json")["synced_at"] == "2026-03-22T06:30:00"

    # 没有任何条目的榜单+周期，抓取时间为 None
    empty_page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="uncensored",
        period="daily",
        page=1,
        page_size=20,
    )
    assert empty_page.total == 0
    assert empty_page.synced_at is None


def _seed_heat_ranking_items(
    *,
    source_key: str = "javdb",
    board_key: str = "censored",
    period: str = "daily",
    items: list[tuple[str, str, int, int]] | None = None,
) -> dict[str, Movie]:
    """构造 (movie_number, javdb_id, rank, heat) 四元组对应的 Movie + RankingItem。"""
    rows = items or []
    created: dict[str, Movie] = {}
    for movie_number, javdb_id, rank, heat in rows:
        movie = _create_movie(movie_number, javdb_id, heat=heat)
        created[movie_number] = movie
        RankingItem.create(
            source_key=source_key,
            board_key=board_key,
            period=period,
            rank=rank,
            movie_number=movie.movie_number,
            movie=movie,
        )
    return created


def test_ranking_catalog_service_sorts_by_heat_desc(app):
    _seed_heat_ranking_items(
        items=[
            ("ABP-001", "MovieA1", 1, 10),
            ("ABP-002", "MovieA2", 2, 50),
            ("ABP-003", "MovieA3", 3, 30),
        ]
    )

    page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
        sort="heat:desc",
    )

    assert page.total == 3
    # 按热度降序：50 > 30 > 10
    assert [item.movie_number for item in page.items] == ["ABP-002", "ABP-003", "ABP-001"]


def test_ranking_catalog_service_sorts_by_heat_asc_with_pagination(app):
    _seed_heat_ranking_items(
        items=[
            ("ABP-001", "MovieA1", 1, 10),
            ("ABP-002", "MovieA2", 2, 50),
            ("ABP-003", "MovieA3", 3, 30),
        ]
    )

    first_page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=2,
        sort="heat:asc",
    )
    second_page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=2,
        page_size=2,
        sort="heat:asc",
    )

    # 按热度升序：10 < 30 < 50，并验证翻页能取到剩余条目
    assert [item.movie_number for item in first_page.items] == ["ABP-001", "ABP-003"]
    assert [item.movie_number for item in second_page.items] == ["ABP-002"]
    assert first_page.total == 3
    assert second_page.total == 3


def test_ranking_catalog_service_sort_heat_tie_breaker_by_rank(app):
    # heat 相同时按 rank 兜底，保证翻页稳定
    _seed_heat_ranking_items(
        items=[
            ("ABP-001", "MovieA1", 1, 20),
            ("ABP-002", "MovieA2", 2, 20),
            ("ABP-003", "MovieA3", 3, 20),
        ]
    )

    desc_page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
        sort="heat:desc",
    )
    asc_page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
        sort="heat:asc",
    )

    # heat 相同时：desc 方向上 rank 也按 desc 兜底；asc 方向上 rank 按 asc 兜底
    assert [item.movie_number for item in desc_page.items] == ["ABP-003", "ABP-002", "ABP-001"]
    assert [item.movie_number for item in asc_page.items] == ["ABP-001", "ABP-002", "ABP-003"]


def test_ranking_catalog_service_default_sort_preserves_rank_order(app):
    # 不传 sort 时保持现有按 rank 升序的默认行为
    _seed_heat_ranking_items(
        items=[
            ("ABP-001", "MovieA1", 2, 99),
            ("ABP-002", "MovieA2", 1, 1),
        ]
    )

    page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
    )

    assert [item.rank for item in page.items] == [1, 2]
    assert [item.movie_number for item in page.items] == ["ABP-002", "ABP-001"]


def test_ranking_catalog_service_sort_by_rank_desc(app):
    _seed_heat_ranking_items(
        items=[
            ("ABP-001", "MovieA1", 1, 10),
            ("ABP-002", "MovieA2", 2, 50),
            ("ABP-003", "MovieA3", 3, 30),
        ]
    )

    page = RankingCatalogService.list_board_items(
        source_key="javdb",
        board_key="censored",
        period="daily",
        page=1,
        page_size=20,
        sort="rank:desc",
    )

    assert [item.rank for item in page.items] == [3, 2, 1]


def test_ranking_catalog_service_rejects_invalid_sort(app):
    _seed_heat_ranking_items(
        items=[("ABP-001", "MovieA1", 1, 10)]
    )

    invalid_sorts = ["heat", "unknown:desc", "heat:weird", ":desc", "heat:"]
    for sort_value in invalid_sorts:
        with_error = None
        try:
            RankingCatalogService.list_board_items(
                source_key="javdb",
                board_key="censored",
                period="daily",
                page=1,
                page_size=20,
                sort=sort_value,
            )
        except ApiError as exc:
            with_error = exc
        assert with_error is not None, f"sort={sort_value!r} 未触发异常"
        assert with_error.status_code == 422
        assert with_error.code == "invalid_ranking_filter"


def test_ranking_catalog_service_validates_period(app):
    with_error = None
    try:
        RankingCatalogService.list_board_items(
            source_key="javdb",
            board_key="censored",
            period=None,
            page=1,
            page_size=20,
        )
    except ApiError as exc:
        with_error = exc
    assert with_error is not None
    assert with_error.status_code == 422
    assert with_error.code == "invalid_ranking_period"


def test_ranking_sync_service_sync_board_period_supports_playback_boards(app):
    # 播放榜走 playback 接口入库，与普通 javdb 榜单一致的删旧插新语义。
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_playback("all", "daily", ["ABP-101"])
    javdb_provider.set_playback("high_score", "daily", ["ABP-102"])
    javdb_provider.set_detail("ABP-101", "MoviePlay101")
    javdb_provider.set_detail("ABP-102", "MoviePlay102")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider},
    )

    all_stats = service.sync_board_period("javdb", "playback_all", "daily")
    high_stats = service.sync_board_period("javdb", "playback_high_score", "daily")

    assert all_stats["stored_items"] == 1
    assert high_stats["stored_items"] == 1
    assert [
        item.movie_number
        for item in RankingItem.select()
        .where(RankingItem.board_key == "playback_all", RankingItem.period == "daily")
    ] == ["ABP-101"]
    assert [
        item.movie_number
        for item in RankingItem.select()
        .where(RankingItem.board_key == "playback_high_score", RankingItem.period == "daily")
    ] == ["ABP-102"]


def test_ranking_sync_service_sync_all_rankings_includes_playback_and_missav_targets(app, monkeypatch):
    # 未配账号时 top250 被排除：javdb 普通 3 榜 + 播放 2 榜各 3 周期，加 missav 1 榜 3 周期 = 18。
    monkeypatch.setattr(settings.metadata, "javdb_username", None)
    monkeypatch.setattr(settings.metadata, "javdb_password", None)

    javdb_provider = FakeRankingProvider()
    javdb_provider.set_ranking("0", "daily", ["ABP-001"])
    javdb_provider.set_playback("all", "daily", ["ABP-003"])
    javdb_provider.set_detail("ABP-001", "MovieA1")
    javdb_provider.set_detail("ABP-003", "MovieA3")

    missav_provider = FakeMissavRankingProvider()
    missav_provider.set_ranking("daily", ["ABP-002"])
    javdb_provider.set_detail("ABP-002", "MovieA2")

    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider, "missav": missav_provider},
    )

    stats = service.sync_all_rankings()

    assert stats["total_targets"] == 18
    assert stats["success_targets"] == 18
    assert stats["failed_targets"] == 0
    # 未配账号：top250 从不抓取。
    assert javdb_provider.top_calls == []
    assert RankingItem.select().where(RankingItem.board_key == "top250").count() == 0
    # 播放榜随整体同步入库。
    assert (
        RankingItem.select()
        .where(RankingItem.board_key.in_(["playback_all", "playback_high_score"]))
        .count()
        == 1
    )


def test_ranking_sync_service_sync_board_period_supports_top250(app):
    # top250 走 /api/v1/movies/top：all 子榜与年度榜各自按 period 入库。
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_top("all", "", ["TOP-001", "TOP-002"])
    javdb_provider.set_top("year", "2024", ["YR-001"])
    javdb_provider.set_detail("TOP-001", "MovieTop1")
    javdb_provider.set_detail("TOP-002", "MovieTop2")
    javdb_provider.set_detail("YR-001", "MovieYr1")
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider},
    )

    all_stats = service.sync_board_period("javdb", "top250", "all")
    year_stats = service.sync_board_period("javdb", "top250", "2024")

    assert all_stats["stored_items"] == 2
    assert year_stats["stored_items"] == 1
    assert ("all", "") in javdb_provider.top_calls
    assert ("year", "2024") in javdb_provider.top_calls
    assert [
        item.movie_number
        for item in RankingItem.select()
        .where(RankingItem.board_key == "top250", RankingItem.period == "all")
        .order_by(RankingItem.rank.asc())
    ] == ["TOP-001", "TOP-002"]
    assert [
        item.movie_number
        for item in RankingItem.select()
        .where(RankingItem.board_key == "top250", RankingItem.period == "2024")
    ] == ["YR-001"]


def test_sync_all_rankings_top250_skips_existing_historical_year(app, monkeypatch):
    # 已配账号：当前年与固定子榜每次抓，历史年份库里已有则跳过。
    monkeypatch.setattr(settings.metadata, "javdb_username", "user@example.com")
    monkeypatch.setattr(settings.metadata, "javdb_password", "secret")
    current_year = str(runtime_now().year)

    # 预置一条历史年份(2020)的 top250 数据，验证不再重抓。
    seeded_movie = _create_movie("OLD-2020", "MovieOld2020")
    RankingItem.create(
        source_key="javdb",
        board_key="top250",
        period="2020",
        rank=1,
        movie_number=seeded_movie.movie_number,
        movie=seeded_movie,
    )

    javdb_provider = FakeRankingProvider()  # 所有 top 子榜默认返回空，专注于“调用与否”
    missav_provider = FakeMissavRankingProvider()
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider, "missav": missav_provider},
    )

    service.sync_all_rankings()

    # 固定子榜 + 当前年被抓取，历史年份 2020 因已有数据被跳过。
    assert ("all", "") in javdb_provider.top_calls
    assert ("video_type", "0") in javdb_provider.top_calls
    assert ("year", current_year) in javdb_provider.top_calls
    assert ("year", "2020") not in javdb_provider.top_calls
    # 预置的 2020 数据原样保留。
    assert (
        RankingItem.select()
        .where(RankingItem.board_key == "top250", RankingItem.period == "2020")
        .count()
        == 1
    )


def test_sync_all_rankings_notifies_once_on_top250_login_failure(app, monkeypatch):
    # 已配账号但登录失败：只发一条通知、top250 全部跳过、不计入 failed_targets。
    monkeypatch.setattr(settings.metadata, "javdb_username", "user@example.com")
    monkeypatch.setattr(settings.metadata, "javdb_password", "secret")

    notify_calls = {"count": 0, "task_run_id": "unset"}

    def _fake_notify(cls, *, related_task_run_id=None):
        notify_calls["count"] += 1
        notify_calls["task_run_id"] = related_task_run_id

    monkeypatch.setattr(
        ActivityService,
        "create_ranking_account_error_notification",
        classmethod(_fake_notify),
    )

    javdb_provider = FakeRankingProvider()
    javdb_provider.top_auth_error = True
    missav_provider = FakeMissavRankingProvider()
    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider, "missav": missav_provider},
    )

    stats = service.sync_all_rankings(task_run_id=7)

    assert notify_calls["count"] == 1  # 多个 top250 目标登录失败，只发一次
    assert notify_calls["task_run_id"] == 7
    assert stats["failed_targets"] == 0  # 登录失败不计入 failed_targets
    assert RankingItem.select().where(RankingItem.board_key == "top250").count() == 0
