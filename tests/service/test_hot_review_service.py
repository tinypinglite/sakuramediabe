from src.api.exception.errors import ApiError
from src.model import HotReviewItem, Image, Media, Movie
from sakuramedia_metadata_providers.models import JavdbMovieReviewResource
from src.service.discovery.hot_review_service import HotReviewCatalogService, HotReviewSyncService


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _build_review(review_id: int, movie_number: str) -> JavdbMovieReviewResource:
    return JavdbMovieReviewResource.model_validate(
        {
            "id": review_id,
            "score": 5,
            "content": f"review-{review_id}",
            "created_at": "2026-03-21T00:00:00Z",
            "username": f"user-{review_id}",
            "like_count": 10,
            "watch_count": 20,
            "movie": {
                "id": f"javdb-{movie_number}",
                "number": movie_number,
                "title": movie_number,
            },
        }
    )


class FakeHotReviewProvider:
    def __init__(self):
        self.pages: dict[str, list[list[JavdbMovieReviewResource]]] = {}
        self.details: dict[str, dict] = {}

    def set_reviews(self, period: str, pages: list[list[JavdbMovieReviewResource]]) -> None:
        self.pages[period] = pages

    def set_detail(self, movie_number: str, javdb_id: str) -> None:
        self.details[movie_number] = {
            "javdb_id": javdb_id,
            "movie_number": movie_number,
            "title": movie_number,
        }

    def get_hot_reviews(self, period: str = "weekly", page: int = 1, limit: int = 24):
        period_pages = self.pages.get(period, [])
        if page <= 0 or page > len(period_pages):
            return []
        return list(period_pages[page - 1])

    def get_movie_by_number(self, movie_number: str) -> dict:
        return self.details[movie_number]


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


def test_hot_review_sync_service_replaces_period_items_with_full_pages(app):
    provider = FakeHotReviewProvider()
    provider.set_reviews(
        "weekly",
        [
            [_build_review(1, "ABP-001"), _build_review(2, "ABP-002")],
            [_build_review(3, "ABP-003")],
        ],
    )
    provider.set_detail("ABP-001", "javdb-abp001")
    provider.set_detail("ABP-002", "javdb-abp002")
    provider.set_detail("ABP-003", "javdb-abp003")
    service = HotReviewSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": provider},
    )

    first_stats = service.sync_period("javdb", "weekly")

    assert first_stats["fetched_reviews"] == 2
    assert first_stats["imported_movies"] == 2
    assert first_stats["stored_items"] == 2
    assert [
        (item.rank, item.review_id, item.movie_number)
        for item in HotReviewItem.select().where(
            HotReviewItem.source_key == "javdb",
            HotReviewItem.period == "weekly",
        ).order_by(HotReviewItem.rank.asc())
    ] == [
        (1, 1, "ABP-001"),
        (2, 2, "ABP-002"),
    ]

    provider.set_reviews("weekly", [[_build_review(11, "ABP-011")]])
    provider.set_detail("ABP-011", "javdb-abp011")
    second_stats = service.sync_period("javdb", "weekly")

    assert second_stats["fetched_reviews"] == 1
    assert second_stats["imported_movies"] == 1
    assert second_stats["stored_items"] == 1
    assert [
        (item.rank, item.review_id, item.movie_number)
        for item in HotReviewItem.select().where(
            HotReviewItem.source_key == "javdb",
            HotReviewItem.period == "weekly",
        ).order_by(HotReviewItem.rank.asc())
    ] == [
        (1, 11, "ABP-011"),
    ]


def test_hot_review_sync_service_skips_reviews_when_movie_import_fails(app):
    provider = FakeHotReviewProvider()
    provider.set_reviews(
        "weekly",
        [[
            _build_review(1, "ABP-001"),
            _build_review(2, "ABP-404"),
            _build_review(3, "ABP-002"),
        ]],
    )
    provider.set_detail("ABP-001", "javdb-abp001")
    provider.set_detail("ABP-404", "javdb-abp404")
    provider.set_detail("ABP-002", "javdb-abp002")
    service = HotReviewSyncService(
        import_service=FakeCatalogImportService(fail_on={"ABP-404"}),
        providers={"javdb": provider},
    )

    stats = service.sync_period("javdb", "weekly")

    assert stats["fetched_reviews"] == 3
    assert stats["imported_movies"] == 2
    assert stats["skipped_reviews"] == 1
    assert stats["stored_items"] == 2
    assert [
        item.movie_number
        for item in HotReviewItem.select().where(
            HotReviewItem.source_key == "javdb",
            HotReviewItem.period == "weekly",
        ).order_by(HotReviewItem.rank.asc())
    ] == ["ABP-001", "ABP-002"]


def test_hot_review_sync_service_stops_when_page_has_no_new_reviews(app):
    provider = FakeHotReviewProvider()
    page_1 = [_build_review(1, "ABP-001"), _build_review(2, "ABP-002")]
    # 第二页与第一页完全重复，模拟远端分页循环。
    page_2 = [_build_review(1, "ABP-001"), _build_review(2, "ABP-002")]
    provider.set_reviews("weekly", [page_1, page_2, [_build_review(3, "ABP-003")]])
    provider.set_detail("ABP-001", "javdb-abp001")
    provider.set_detail("ABP-002", "javdb-abp002")
    provider.set_detail("ABP-003", "javdb-abp003")
    service = HotReviewSyncService(
        import_service=FakeCatalogImportService(),
        providers={"javdb": provider},
    )
    service.HOT_REVIEW_PAGE_SIZE = 2

    stats = service.sync_period("javdb", "weekly")

    assert stats["fetched_reviews"] == 2
    assert stats["imported_movies"] == 2
    assert stats["stored_items"] == 2
    assert [
        (item.rank, item.review_id, item.movie_number)
        for item in HotReviewItem.select().where(
            HotReviewItem.source_key == "javdb",
            HotReviewItem.period == "weekly",
        ).order_by(HotReviewItem.rank.asc())
    ] == [
        (1, 1, "ABP-001"),
        (2, 2, "ABP-002"),
    ]


def test_hot_review_catalog_service_lists_items_by_rank_with_movie_state(app):
    thin_cover_image = Image.create(
        origin="hot-review-service/thin-origin.jpg",
        small="hot-review-service/thin-small.jpg",
        medium="hot-review-service/thin-medium.jpg",
        large="hot-review-service/thin-large.jpg",
    )
    movie_a = _create_movie(
        "ABP-001",
        "javdb-abp001",
        title_zh="热评服务中文 A",
        thin_cover_image=thin_cover_image,
    )
    movie_b = _create_movie("ABP-002", "javdb-abp002")
    Media.create(movie=movie_b, path="/library/main/abp-002.mp4", valid=True)
    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=2,
        review_id=12,
        movie_number=movie_b.movie_number,
        movie=movie_b,
        score=4,
        content="content-b",
        review_created_at="2026-03-21T01:00:00Z",
        username="user-b",
        like_count=12,
        watch_count=22,
    )
    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=1,
        review_id=11,
        movie_number=movie_a.movie_number,
        movie=movie_a,
        score=5,
        content="content-a",
        review_created_at="2026-03-21T00:00:00Z",
        username="user-a",
        like_count=11,
        watch_count=21,
    )

    page = HotReviewCatalogService.list_items(
        period="weekly",
        page=1,
        page_size=20,
    )

    assert page.total == 2
    assert [item.rank for item in page.items] == [1, 2]
    assert [item.review_id for item in page.items] == [11, 12]
    assert [item.movie.movie_number for item in page.items] == ["ABP-001", "ABP-002"]
    assert page.items[0].movie.title_zh == "热评服务中文 A"
    assert page.items[0].movie.thin_cover_image is not None
    assert page.items[0].movie.thin_cover_image.id == thin_cover_image.id
    assert page.items[1].movie.thin_cover_image is None
    assert page.items[0].movie.can_play is False
    assert page.items[1].movie.can_play is True
    assert [item.model_dump(mode="json")["created_at"] for item in page.items] == [
        "2026-03-21T00:00:00",
        "2026-03-21T01:00:00",
    ]


def test_hot_review_catalog_service_validates_period(app):
    with_error = None
    try:
        HotReviewCatalogService.list_items(
            period="daily",
            page=1,
            page_size=20,
        )
    except ApiError as exc:
        with_error = exc
    assert with_error is not None
    assert with_error.status_code == 422
    assert with_error.code == "invalid_hot_review_period"
