from src.api.exception.errors import ApiError
from src.model import Image, Media, Movie, RankingItem
from src.service.discovery.ranking_service import RankingCatalogService, RankingSyncService


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
        self.details: dict[str, dict] = {}

    def set_ranking(self, video_type: str, period: str, numbers: list[str]) -> None:
        self.rankings[(video_type, period)] = numbers

    def set_detail(self, movie_number: str, javdb_id: str) -> None:
        self.details[movie_number] = {
            "javdb_id": javdb_id,
            "movie_number": movie_number,
            "title": movie_number,
        }

    def get_rank_numbers(self, video_type: str, period: str = "daily") -> list[str]:
        return list(self.rankings.get((video_type, period), []))

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


def test_ranking_sync_service_sync_all_rankings_includes_missav_targets(app):
    javdb_provider = FakeRankingProvider()
    javdb_provider.set_ranking("0", "daily", ["ABP-001"])
    javdb_provider.set_ranking("0", "weekly", [])
    javdb_provider.set_ranking("0", "monthly", [])
    javdb_provider.set_ranking("1", "daily", [])
    javdb_provider.set_ranking("1", "weekly", [])
    javdb_provider.set_ranking("1", "monthly", [])
    javdb_provider.set_ranking("3", "daily", [])
    javdb_provider.set_ranking("3", "weekly", [])
    javdb_provider.set_ranking("3", "monthly", [])
    javdb_provider.set_detail("ABP-001", "MovieA1")

    missav_provider = FakeMissavRankingProvider()
    missav_provider.set_ranking("daily", ["ABP-002"])
    missav_provider.set_ranking("weekly", [])
    missav_provider.set_ranking("monthly", [])
    javdb_provider.set_detail("ABP-002", "MovieA2")

    service = RankingSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": javdb_provider, "missav": missav_provider},
    )

    stats = service.sync_all_rankings()

    assert stats["total_targets"] == 12
    assert stats["success_targets"] == 12
    assert stats["failed_targets"] == 0
