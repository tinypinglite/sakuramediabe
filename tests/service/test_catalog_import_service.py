import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.config.config import settings
from src.model import (
    Actor,
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Tag,
)
from src.schema.metadata.javdb import JavdbMovieActorResource, JavdbMovieDetailResource, JavdbMovieTagResource
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError


@pytest.fixture()
def import_tables(test_db):
    models = [
        Image,
        Tag,
        Actor,
        Movie,
        MovieActor,
        MovieTag,
        MoviePlotImage,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _build_detail(
    movie_number: str,
    plot_images: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        cover_image="https://example.com/cover.jpg",
        release_date="2024-05-20",
        duration_minutes=120,
        score=4.4,
        watched_count=11,
        want_watch_count=12,
        comment_count=13,
        score_number=14,
        is_subscribed=False,
        summary="summary",
        series_name="series",
        extra=extra
        if extra is not None
        else {
            "success": 1,
            "data": {
                "movie": {
                    "id": f"javdb-{movie_number}",
                    "number": movie_number,
                    "title": f"title-{movie_number}",
                }
            },
        },
        actors=[
            JavdbMovieActorResource(
                javdb_id=f"actor-{movie_number}",
                name="actor-a",
                avatar_url="https://example.com/actor-a.jpg",
                gender=1,
            )
        ],
        tags=[
            JavdbMovieTagResource(javdb_id=f"tag-{movie_number}", name="剧情"),
        ],
        plot_images=plot_images if plot_images is not None else ["https://example.com/plot-1.jpg"],
    )


def _fake_downloader(url: str, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(f"downloaded:{url}".encode("utf-8"))


class _FakeDownloadResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


def test_upsert_movie_from_javdb_detail_creates_catalog_records(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    service = CatalogImportService(image_downloader=_fake_downloader)

    movie = service.upsert_movie_from_javdb_detail(detail)

    assert movie.movie_number == "ABP-123"
    assert Movie.select().count() == 1
    assert Actor.select().count() == 1
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 1
    assert Image.select().count() == 3
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"
    assert movie.extra == detail.extra
    assert movie.subscribed_at is None
    actor = Actor.get()
    assert actor.profile_image is not None
    assert actor.profile_image.origin == "actors/actor-ABP-123.jpg"
    plot_images = [
        link.image.origin
        for link in MoviePlotImage.select(MoviePlotImage, Image).join(Image).order_by(MoviePlotImage.id)
    ]
    assert plot_images == ["movies/ABP-123/plots/0.jpg"]


def test_upsert_movie_from_javdb_detail_is_idempotent_for_links(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)
    service.upsert_movie_from_javdb_detail(detail)

    assert Movie.select().count() == 1
    assert Actor.select().count() == 1
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 1
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"


def test_upsert_movie_from_javdb_detail_overwrites_extra_with_latest_payload(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    old_detail = _build_detail(
        "ABP-123",
        extra={"success": 1, "data": {"movie": {"id": "javdb-ABP-123", "number": "ABP-123", "title": "old"}}},
    )
    new_detail = _build_detail(
        "ABP-123",
        extra={"success": 1, "data": {"movie": {"id": "javdb-ABP-123", "number": "ABP-123", "title": "new"}}},
    )
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(old_detail)
    service.upsert_movie_from_javdb_detail(new_detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.extra == new_detail.extra


def test_upsert_movie_from_javdb_detail_sets_subscribed_at_for_new_subscribed_movie(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    detail.is_subscribed = True
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.subscribed_at is not None


def test_upsert_movie_from_javdb_detail_sets_subscribed_at_when_movie_becomes_subscribed(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123"))

    subscribed_detail = _build_detail("ABP-123")
    subscribed_detail.is_subscribed = True
    service.upsert_movie_from_javdb_detail(subscribed_detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.subscribed_at is not None


def test_upsert_movie_from_javdb_detail_keeps_existing_subscribed_at_when_already_subscribed(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    detail.is_subscribed = True
    service = CatalogImportService(image_downloader=_fake_downloader)
    service.upsert_movie_from_javdb_detail(detail)

    original_timestamp = datetime(2026, 3, 8, 9, 0, 0)
    Movie.update(subscribed_at=original_timestamp).where(Movie.movie_number == "ABP-123").execute()
    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.subscribed_at == original_timestamp


def test_upsert_movie_from_javdb_detail_preserves_existing_subscription_when_detail_subscription_is_unknown(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    original_timestamp = datetime(2026, 3, 8, 9, 0, 0)
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_subscribed=True,
        subscribed_at=original_timestamp,
    )
    detail = _build_detail("ABP-123")
    detail.is_subscribed = None
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_timestamp


def test_upsert_movie_from_javdb_detail_clears_subscribed_at_when_movie_becomes_unsubscribed(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    detail.is_subscribed = True
    service = CatalogImportService(image_downloader=_fake_downloader)
    service.upsert_movie_from_javdb_detail(detail)

    unsubscribed_detail = _build_detail("ABP-123")
    unsubscribed_detail.is_subscribed = False
    service.upsert_movie_from_javdb_detail(unsubscribed_detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.subscribed_at is None


def test_upsert_movie_from_javdb_detail_sets_collection_when_movie_number_matches_configured_prefix(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    detail = _build_detail("OFJE-123")
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "OFJE-123")
    assert movie.is_collection is True


def test_upsert_movie_from_javdb_detail_clears_collection_when_movie_number_does_not_match_configured_prefix(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_collection=True,
    )
    detail = _build_detail("ABP-123")
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_collection is False


def test_upsert_movie_from_javdb_detail_force_subscribed_marks_movie_as_subscribed(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123"), force_subscribed=True)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None


def test_upsert_movie_from_javdb_detail_force_subscribed_updates_existing_unsubscribed_movie(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123"))

    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123"), force_subscribed=True)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None


def test_upsert_movie_from_javdb_detail_force_subscribed_keeps_existing_subscribed_at(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    detail = _build_detail("ABP-123")
    detail.is_subscribed = True
    service.upsert_movie_from_javdb_detail(detail)

    original_timestamp = datetime(2026, 3, 8, 9, 0, 0)
    Movie.update(subscribed_at=original_timestamp).where(Movie.movie_number == "ABP-123").execute()
    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123"), force_subscribed=True)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_timestamp


def test_upsert_movie_from_javdb_detail_downloads_all_images_concurrently(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-123",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    service = CatalogImportService(image_downloader=_fake_downloader)
    expected_parallel_downloads = 4
    started_urls: List[str] = []
    lock = threading.Lock()
    ready = threading.Event()
    release = threading.Event()

    def _concurrent_downloader(url: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with lock:
            started_urls.append(url)
            if len(started_urls) == expected_parallel_downloads:
                ready.set()
        release.wait(timeout=2)
        target_path.write_bytes(f"downloaded:{url}".encode("utf-8"))

    service.image_downloader = _concurrent_downloader

    worker = threading.Thread(target=service.upsert_movie_from_javdb_detail, args=(detail,))
    worker.start()

    assert ready.wait(timeout=1), "expected image downloads to overlap"
    with lock:
        assert len(started_urls) == expected_parallel_downloads

    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert Movie.select().count() == 1
    assert MoviePlotImage.select().count() == 2
    assert Image.select().count() == 4
    assert set(started_urls) == {
        "https://example.com/cover.jpg",
        "https://example.com/plot-1.jpg",
        "https://example.com/plot-2.jpg",
        "https://example.com/actor-a.jpg",
    }
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"


def test_upsert_movie_from_javdb_detail_deduplicates_plot_image_downloads(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-123",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    downloaded_urls: List[str] = []

    def _tracking_downloader(url: str, target_path: Path) -> None:
        downloaded_urls.append(url)
        _fake_downloader(url, target_path)

    service = CatalogImportService(image_downloader=_tracking_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    assert downloaded_urls.count("https://example.com/plot-1.jpg") == 1
    assert downloaded_urls.count("https://example.com/plot-2.jpg") == 1
    assert MoviePlotImage.select().count() == 2
    assert Image.select().count() == 4
    plot_images = [
        link.image.origin
        for link in MoviePlotImage.select(MoviePlotImage, Image).join(Image).order_by(MoviePlotImage.id)
    ]
    assert plot_images == [
        "movies/ABP-123/plots/0.jpg",
        "movies/ABP-123/plots/1.jpg",
    ]


def test_upsert_movie_from_javdb_detail_skips_existing_movie_images(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-123",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    image_root = tmp_path / "images"
    cover_path = image_root / "movies" / "ABP-123" / "cover.jpg"
    plot_path = image_root / "movies" / "ABP-123" / "plots" / "0.jpg"
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    cover_path.write_bytes(b"cover")
    plot_path.write_bytes(b"plot-0")
    downloaded_urls: List[str] = []

    def _tracking_downloader(url: str, target_path: Path) -> None:
        downloaded_urls.append(url)
        _fake_downloader(url, target_path)

    service = CatalogImportService(image_downloader=_tracking_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    assert "https://example.com/cover.jpg" not in downloaded_urls
    assert "https://example.com/plot-1.jpg" not in downloaded_urls
    assert "https://example.com/plot-2.jpg" in downloaded_urls
    assert MoviePlotImage.select().count() == 2
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"


def test_upsert_movie_from_javdb_detail_preserves_existing_cover_when_detail_cover_is_missing(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    existing_cover = Image.create(
        origin="existing-cover.jpg",
        small="existing-cover.jpg",
        medium="existing-cover.jpg",
        large="existing-cover.jpg",
    )
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        cover_image=existing_cover,
    )
    detail = _build_detail("ABP-123")
    detail.cover_image = None
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image_id == existing_cover.id


def test_upsert_actor_from_javdb_resource_updates_name_gender_without_overwriting_alias(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)

    created = service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-a",
            avatar_url=None,
            gender=1,
        )
    )
    updated = service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-b",
            avatar_url=None,
            gender=2,
        )
    )

    assert created.id == updated.id
    actor = Actor.get(Actor.javdb_id == "actor-1")
    assert actor.name == "name-b"
    assert actor.alias_name == "name-a"
    assert actor.gender == 2


def test_upsert_actor_from_javdb_resource_does_not_change_subscription_markers(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    synced_at = datetime(2026, 3, 10, 9, 0, 0)
    full_synced_at = datetime(2026, 3, 8, 9, 0, 0)
    Actor.create(
        javdb_id="actor-1",
        name="name-a",
        alias_name="alias-a",
        gender=1,
        is_subscribed=True,
        subscribed_movies_synced_at=synced_at,
        subscribed_movies_full_synced_at=full_synced_at,
    )
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-b",
            avatar_url=None,
            gender=2,
        )
    )

    actor = Actor.get(Actor.javdb_id == "actor-1")
    assert actor.is_subscribed is True
    assert actor.subscribed_movies_synced_at == synced_at
    assert actor.subscribed_movies_full_synced_at == full_synced_at


def test_upsert_movie_from_javdb_detail_uses_prepared_actor_avatar_tasks(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    detail = _build_detail("ABP-123")

    def _unexpected_persist_image(*args, **kwargs):
        raise AssertionError("_persist_image should not be used during movie import")

    monkeypatch.setattr(service, "_persist_image", _unexpected_persist_image)

    movie = service.upsert_movie_from_javdb_detail(detail)

    actor = Actor.get(Actor.javdb_id == "actor-ABP-123")
    assert movie.movie_number == "ABP-123"
    assert actor.profile_image is not None
    assert actor.profile_image.origin == "actors/actor-ABP-123.jpg"


def test_upsert_movie_from_javdb_detail_accepts_missing_cover_plot_and_actor_images(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    detail = _build_detail("ABP-123", plot_images=[])
    detail.cover_image = None
    detail.actors[0].avatar_url = None

    movie = service.upsert_movie_from_javdb_detail(detail)

    actor = Actor.get(Actor.javdb_id == "actor-ABP-123")
    assert movie.cover_image is None
    assert actor.profile_image is None
    assert MoviePlotImage.select().count() == 0
    assert Image.select().count() == 0


def test_upsert_movie_rolls_back_database_records_when_image_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    def _broken_downloader(url: str, target_path: Path) -> None:
        raise ImageDownloadError("network down")

    detail = _build_detail("ABP-123", plot_images=["https://example.com/plot-1.jpg"])
    detail.cover_image = None
    detail.actors[0].avatar_url = None
    service = CatalogImportService(image_downloader=_broken_downloader)

    with pytest.raises(ImageDownloadError):
        service.upsert_movie_from_javdb_detail(detail)

    assert Movie.select().count() == 0
    assert Actor.select().count() == 0
    assert Tag.select().count() == 0
    assert MovieActor.select().count() == 0
    assert MovieTag.select().count() == 0
    assert MoviePlotImage.select().count() == 0


def test_upsert_movie_rolls_back_database_records_when_concurrent_movie_image_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-123",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    detail.actors[0].avatar_url = None
    failed_urls: List[str] = []

    def _partially_broken_downloader(url: str, target_path: Path) -> None:
        if url.endswith("plot-2.jpg"):
            failed_urls.append(url)
            raise ImageDownloadError("network down")
        _fake_downloader(url, target_path)

    service = CatalogImportService(image_downloader=_partially_broken_downloader)

    with pytest.raises(ImageDownloadError):
        service.upsert_movie_from_javdb_detail(detail)

    assert failed_urls == ["https://example.com/plot-2.jpg"]
    assert Movie.select().count() == 0
    assert Actor.select().count() == 0
    assert Tag.select().count() == 0
    assert MovieActor.select().count() == 0
    assert MovieTag.select().count() == 0
    assert MoviePlotImage.select().count() == 0
    assert Image.select().count() == 0


def test_download_image_retries_until_success(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = CatalogImportService()
    target_path = tmp_path / "retry.jpg"
    attempts: Dict[str, int] = {"count": 0}

    def _fake_request(method: str, url: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.TimeoutException("timeout")
        if attempts["count"] == 2:
            return _FakeDownloadResponse(status_code=503, content=b"")
        return _FakeDownloadResponse(status_code=200, content=b"ok")

    monkeypatch.setattr(service.http_client, "request", _fake_request)
    service._download_image("https://example.com/retry.jpg", target_path)

    assert attempts["count"] == 3
    assert target_path.read_bytes() == b"ok"


def test_download_image_skips_when_target_already_exists(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = CatalogImportService()
    target_path = tmp_path / "exists.jpg"
    target_path.write_bytes(b"cached")

    def _fake_request(method: str, url: str):
        raise AssertionError("http client should not be called for existing files")

    monkeypatch.setattr(service.http_client, "request", _fake_request)

    service._download_image("https://example.com/exists.jpg", target_path)

    assert target_path.read_bytes() == b"cached"


def test_download_image_raises_after_retry_exhausted(monkeypatch: pytest.MonkeyPatch, tmp_path):
    service = CatalogImportService()
    target_path = tmp_path / "fail.jpg"

    def _fake_request(method: str, url: str):
        return _FakeDownloadResponse(status_code=500, content=b"")

    monkeypatch.setattr(service.http_client, "request", _fake_request)

    with pytest.raises(ImageDownloadError):
        service._download_image("https://example.com/fail.jpg", target_path)
