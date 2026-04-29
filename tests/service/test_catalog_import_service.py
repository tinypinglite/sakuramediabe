import threading
from datetime import datetime
import io
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import httpx
import pytest
from PIL import Image as PillowImage

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    ResourceTaskState,
    Tag,
)
from sakuramedia_metadata_providers.models import JavdbMovieActorResource, JavdbMovieDetailResource, JavdbMovieTagResource
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError


@pytest.fixture()
def import_tables(test_db):
    models = [
        Image,
        Tag,
        Actor,
        MovieSeries,
        Movie,
        MovieActor,
        MovieTag,
        MoviePlotImage,
        ResourceTaskState,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _build_detail(
    movie_number: str,
    plot_images: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    maker_name: str | None = "S1 NO.1 STYLE",
    director_name: str | None = "嵐山みちる",
    duration_minutes: int = 120,
    series_name: str | None = "series",
) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        cover_image="https://example.com/cover.jpg",
        release_date="2024-05-20",
        duration_minutes=duration_minutes,
        score=4.4,
        watched_count=11,
        want_watch_count=12,
        comment_count=13,
        score_number=14,
        is_subscribed=False,
        summary="summary",
        series_name=series_name,
        maker_name=maker_name,
        director_name=director_name,
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


def _write_image_file(image_root: Path, relative_path: str, content: bytes) -> None:
    target_path = image_root / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(content)


def _build_image_bytes(width: int, height: int, color: tuple[int, int, int] = (120, 80, 40)) -> bytes:
    image = PillowImage.new("RGB", (width, height), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _build_image_downloader(image_bytes_by_url: Dict[str, bytes]):
    def _download(url: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(image_bytes_by_url[url])

    return _download


class _FakeDownloadResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


class FakeDmmProvider:
    def __init__(self, desc_by_number=None, failures=None):
        self.desc_by_number = desc_by_number or {}
        self.failures = failures or {}

    def get_movie_desc(self, movie_number: str) -> str:
        if movie_number in self.failures:
            raise self.failures[movie_number]
        return self.desc_by_number[movie_number]


def _patch_fake_cv2(monkeypatch: pytest.MonkeyPatch, column_gradient: list[float]) -> None:
    numpy = pytest.importorskip("numpy")

    class _FakeCv2Module:
        COLOR_BGR2GRAY = 0
        CV_64F = 0

        @staticmethod
        def cvtColor(image, code):
            return image[:, :, 0]

        @staticmethod
        def Sobel(gray, depth, dx, dy, ksize=3):
            height, width = gray.shape
            gradient = numpy.zeros((height, width), dtype=float)
            gradient[:] = numpy.array(column_gradient, dtype=float)
            return gradient

    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2Module())


def test_detect_split_points_rejects_obviously_asymmetric_points(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((8, 200, 3), dtype=float)
    column_gradient = [0.0] * 200
    column_gradient[95] = 10.0
    column_gradient[150] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (-1, -1)


def test_detect_split_points_accepts_nearly_symmetric_points(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((8, 200, 3), dtype=float)
    column_gradient = [0.0] * 200
    column_gradient[82] = 10.0
    column_gradient[121] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (82, 121)


def test_detect_split_points_accepts_wide_spine_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((538, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[373] = 10.0
    column_gradient[462] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (373, 462)


def test_detect_split_points_accepts_narrow_spine_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((538, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[379] = 10.0
    column_gradient[407] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (379, 407)


def test_detect_split_points_accepts_center_split_portrait_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((565, 800, 3), dtype=float)
    column_gradient = [0.0] * 800
    column_gradient[300] = 10.0
    column_gradient[400] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (300, 400)


def test_detect_split_points_rejects_non_portrait_enhanced_crop(monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    image = numpy.zeros((276, 276, 3), dtype=float)
    column_gradient = [0.0] * 276
    column_gradient[91] = 10.0
    column_gradient[207] = 12.0
    _patch_fake_cv2(monkeypatch, column_gradient)

    left_point, right_point = CatalogImportService._detect_split_points(image)

    assert (left_point, right_point) == (-1, -1)


def test_split_image_writes_portrait_crop_for_wide_spine(tmp_path, monkeypatch: pytest.MonkeyPatch):
    numpy = pytest.importorskip("numpy")
    source_path = tmp_path / "cover.jpg"
    output_path = tmp_path / "thin-cover.jpg"
    source_path.write_bytes(b"cover")
    image = numpy.zeros((538, 800, 3), dtype=numpy.uint8)

    class _FakeCv2Module:
        COLOR_BGR2GRAY = 0
        CV_64F = 0

        @staticmethod
        def imread(path):
            return image.copy() if path == str(source_path) else None

        @staticmethod
        def imwrite(path, cropped_image):
            PillowImage.fromarray(cropped_image).save(path, format="JPEG")
            return True

        @staticmethod
        def cvtColor(source_image, code):
            return source_image[:, :, 0]

        @staticmethod
        def Sobel(gray, depth, dx, dy, ksize=3):
            gradient = numpy.zeros_like(gray, dtype=float)
            gradient[:, 373] = 10.0
            gradient[:, 462] = 12.0
            return gradient

    monkeypatch.setitem(sys.modules, "cv2", _FakeCv2Module())

    assert CatalogImportService._split_image(source_path, output_path) is True

    with PillowImage.open(output_path) as output_image:
        assert output_image.size == (338, 538)


def test_upsert_movie_from_javdb_detail_creates_catalog_records(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123")
    service = CatalogImportService(
        image_downloader=_fake_downloader,
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-123": "movie desc"}),
    )

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
    task_state = ResourceTaskState.get(
        ResourceTaskState.task_key == CatalogImportService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie.id,
    )
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"
    assert movie.desc == "movie desc"
    assert task_state.state == "succeeded"
    assert movie.extra == detail.extra
    assert movie.maker_name == "S1 NO.1 STYLE"
    assert movie.director_name == "嵐山みちる"
    assert movie.subscribed_at is None
    actor = Actor.get()
    assert actor.profile_image is not None
    assert actor.profile_image.origin == "actors/actor-ABP-123.jpg"
    plot_images = [
        link.image.origin
        for link in MoviePlotImage.select(MoviePlotImage, Image).join(Image).order_by(MoviePlotImage.id)
    ]
    assert plot_images == ["movies/ABP-123/plots/0.jpg"]


def test_upsert_movie_from_javdb_detail_generates_thin_cover_from_cover_split(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-200")
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-1.jpg": _build_image_bytes(400, 600),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        ),
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-200": "movie desc"}),
    )

    def _fake_split(image_path: Path, output_image_path: Path, center_range: int = 100) -> bool:
        output_image_path.parent.mkdir(parents=True, exist_ok=True)
        output_image_path.write_bytes(_build_image_bytes(420, 640))
        return True

    monkeypatch.setattr(service, "_split_image", _fake_split)

    movie = service.upsert_movie_from_javdb_detail(detail)
    refreshed_movie = Movie.get_by_id(movie.id)

    assert refreshed_movie.thin_cover_image is not None
    assert refreshed_movie.thin_cover_image.origin == "movies/ABP-200/thin-cover.jpg"
    assert (tmp_path / "images" / "movies/ABP-200/thin-cover.jpg").exists()


def test_upsert_movie_from_javdb_detail_falls_back_to_first_portrait_plot_image(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-201",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-1.jpg": _build_image_bytes(400, 640),
                "https://example.com/plot-2.jpg": _build_image_bytes(800, 600),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        ),
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-201": "movie desc"}),
    )
    monkeypatch.setattr(service, "_split_image", lambda *_args, **_kwargs: False)

    movie = service.upsert_movie_from_javdb_detail(detail)
    refreshed_movie = Movie.get_by_id(movie.id)
    plot_links = list(
        MoviePlotImage.select(MoviePlotImage, Image).join(Image).where(MoviePlotImage.movie == refreshed_movie).order_by(MoviePlotImage.id)
    )

    assert refreshed_movie.thin_cover_image_id == plot_links[0].image.id


def test_upsert_movie_from_javdb_detail_falls_back_to_second_portrait_plot_image(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-202",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-1.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-2.jpg": _build_image_bytes(400, 640),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        ),
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-202": "movie desc"}),
    )
    monkeypatch.setattr(service, "_split_image", lambda *_args, **_kwargs: False)

    movie = service.upsert_movie_from_javdb_detail(detail)
    refreshed_movie = Movie.get_by_id(movie.id)
    plot_links = list(
        MoviePlotImage.select(MoviePlotImage, Image).join(Image).where(MoviePlotImage.movie == refreshed_movie).order_by(MoviePlotImage.id)
    )

    assert refreshed_movie.thin_cover_image_id == plot_links[1].image.id


def test_upsert_movie_from_javdb_detail_clears_thin_cover_when_split_and_fallback_fail(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail(
        "ABP-203",
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-1.jpg": _build_image_bytes(800, 600),
                "https://example.com/plot-2.jpg": _build_image_bytes(900, 600),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        ),
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-203": "movie desc"}),
    )
    monkeypatch.setattr(service, "_split_image", lambda *_args, **_kwargs: False)

    movie = service.upsert_movie_from_javdb_detail(detail)
    refreshed_movie = Movie.get_by_id(movie.id)

    assert refreshed_movie.thin_cover_image is None


def test_upsert_movie_from_javdb_detail_marks_desc_failure_without_blocking_import(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-124")
    service = CatalogImportService(
        image_downloader=_fake_downloader,
        dmm_provider=FakeDmmProvider(
            failures={"ABP-124": MetadataNotFoundError("movie_desc", "ABP-124")}
        ),
    )

    movie = service.upsert_movie_from_javdb_detail(detail)

    assert movie.movie_number == "ABP-124"
    refreshed = Movie.get(Movie.movie_number == "ABP-124")
    task_state = ResourceTaskState.get(
        ResourceTaskState.task_key == CatalogImportService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == refreshed.id,
    )
    assert refreshed.desc == ""
    assert task_state.state == "failed"
    assert task_state.attempt_count == 1
    assert task_state.last_attempted_at is not None
    assert task_state.last_succeeded_at is None
    assert task_state.last_error == "movie_desc not found: ABP-124"
    assert task_state.extra == {"terminal": False}


def test_upsert_movie_from_javdb_detail_marks_desc_failure_as_retryable_when_description_missing(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-126")
    service = CatalogImportService(
        image_downloader=_fake_downloader,
        dmm_provider=FakeDmmProvider(
            failures={"ABP-126": MetadataNotFoundError("movie_desc", "ABP-126")}
        ),
    )

    service.upsert_movie_from_javdb_detail(detail)

    task_state = ResourceTaskState.get(
        ResourceTaskState.task_key == CatalogImportService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == Movie.get(Movie.movie_number == "ABP-126").id,
    )
    assert task_state.state == "failed"
    assert task_state.extra == {"terminal": False}


def test_upsert_movie_from_javdb_detail_preserves_existing_desc_when_refresh_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-125")
    success_service = CatalogImportService(
        image_downloader=_fake_downloader,
        dmm_provider=FakeDmmProvider(desc_by_number={"ABP-125": "first desc"}),
    )
    failure_service = CatalogImportService(
        image_downloader=_fake_downloader,
        dmm_provider=FakeDmmProvider(
            failures={"ABP-125": MetadataNotFoundError("movie_desc", "ABP-125")}
        ),
    )

    success_service.upsert_movie_from_javdb_detail(detail)
    failure_service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-125")
    task_state = ResourceTaskState.get(
        ResourceTaskState.task_key == CatalogImportService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie.id,
    )
    assert movie.desc == "first desc"
    assert task_state.state == "failed"
    assert task_state.attempt_count == 2
    assert task_state.extra == {"terminal": False}


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
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.jpg": _build_image_bytes(900, 600),
                "https://example.com/plot-1.jpg": _build_image_bytes(400, 640),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        )
    )

    def _fake_split(image_path: Path, output_image_path: Path, center_range: int = 100) -> bool:
        output_image_path.parent.mkdir(parents=True, exist_ok=True)
        output_image_path.write_bytes(_build_image_bytes(420, 640))
        return True

    monkeypatch.setattr(service, "_split_image", _fake_split)

    service.upsert_movie_from_javdb_detail(old_detail)
    service.upsert_movie_from_javdb_detail(new_detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.extra == new_detail.extra


def test_upsert_movie_from_javdb_detail_overwrites_staff_with_latest_payload(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    old_detail = _build_detail("ABP-123", maker_name="旧厂商", director_name="旧导演")
    new_detail = _build_detail("ABP-123", maker_name="新厂商", director_name="新导演")
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(old_detail)
    service.upsert_movie_from_javdb_detail(new_detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.maker_name == "新厂商"
    assert movie.director_name == "新导演"


def test_upsert_movie_from_javdb_detail_reuses_and_clears_movie_series(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123", series_name=" series "))
    service.upsert_movie_from_javdb_detail(_build_detail("ABP-124", series_name="series"))

    first_movie = Movie.get(Movie.movie_number == "ABP-123")
    second_movie = Movie.get(Movie.movie_number == "ABP-124")
    assert MovieSeries.select().count() == 1
    assert first_movie.series_id == second_movie.series_id
    assert first_movie.series_name == "series"

    service.upsert_movie_from_javdb_detail(_build_detail("ABP-123", series_name="   "))

    refreshed_movie = Movie.get(Movie.movie_number == "ABP-123")
    assert refreshed_movie.series_id is None
    assert refreshed_movie.series_name is None


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
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
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
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
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


def test_upsert_movie_from_javdb_detail_sets_collection_when_duration_exceeds_threshold(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.media, "others_number_features", set())
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    detail = _build_detail("ABP-123", duration_minutes=301)
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_collection is True


def test_upsert_movie_from_javdb_detail_clears_collection_when_duration_no_longer_exceeds_threshold(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.media, "others_number_features", set())
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_collection=True,
        duration_minutes=360,
    )
    detail = _build_detail("ABP-123", duration_minutes=300)
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_collection is False


def test_upsert_movie_from_javdb_detail_keeps_manual_collection_override(
    import_tables,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.media, "others_number_features", {"OFJE"})
    monkeypatch.setattr(settings.media, "collection_duration_threshold_minutes", 300)
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_collection=True,
        is_collection_overridden=True,
    )
    detail = _build_detail("ABP-123", duration_minutes=120)
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_movie_from_javdb_detail(detail)

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_collection is True
    assert movie.is_collection_overridden is True


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


def test_upsert_actor_from_javdb_resource_merges_authoritative_aliases(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)

    created = service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-a",
            alias_names=["name-a", "alias-a"],
            avatar_url=None,
            gender=1,
        )
    )
    updated = service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-b",
            alias_names=["name-b", "name-a", "alias-b"],
            avatar_url=None,
            gender=2,
        )
    )

    assert created.id == updated.id
    actor = Actor.get(Actor.javdb_id == "actor-1")
    assert actor.name == "name-b"
    assert actor.alias_name == "name-b / name-a / alias-b / alias-a"
    assert actor.gender == 2


def test_upsert_actor_from_javdb_resource_keeps_existing_aliases_when_merging(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    Actor.create(
        javdb_id="actor-1",
        name="old-name",
        alias_name="legacy-a / legacy-b",
        gender=1,
    )
    service = CatalogImportService(image_downloader=_fake_downloader)

    service.upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id="actor-1",
            name="name-b",
            alias_names=["name-b", "三上悠亞", "legacy-a"],
            avatar_url=None,
            gender=2,
        )
    )

    actor = Actor.get(Actor.javdb_id == "actor-1")
    assert actor.alias_name == "name-b / 三上悠亞 / legacy-a / legacy-b"


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


def test_upsert_movie_from_javdb_detail_treats_none_collections_as_empty_lists(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = CatalogImportService(image_downloader=_fake_downloader)
    detail = _build_detail("ABP-123", plot_images=[])
    detail.cover_image = None
    detail.actors = None
    detail.tags = None
    detail.plot_images = None

    movie = service.upsert_movie_from_javdb_detail(detail)

    assert movie.movie_number == "ABP-123"
    assert Actor.select().count() == 0
    assert Tag.select().count() == 0
    assert MovieActor.select().count() == 0
    assert MovieTag.select().count() == 0
    assert MoviePlotImage.select().count() == 0
    assert Image.select().count() == 0


def test_upsert_movie_skips_plot_image_when_image_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    def _broken_downloader(url: str, target_path: Path) -> None:
        raise ImageDownloadError("network down")

    detail = _build_detail("ABP-123", plot_images=["https://example.com/plot-1.jpg"])
    detail.cover_image = None
    detail.actors[0].avatar_url = None
    service = CatalogImportService(image_downloader=_broken_downloader)

    movie = service.upsert_movie_from_javdb_detail(detail)

    assert movie.movie_number == "ABP-123"
    assert Movie.select().count() == 1
    assert Actor.select().count() == 1
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 0


def test_upsert_movie_skips_failed_non_cover_images_when_concurrent_download_fails(
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

    movie = service.upsert_movie_from_javdb_detail(detail)

    assert failed_urls == ["https://example.com/plot-2.jpg"]
    assert movie.movie_number == "ABP-123"
    assert Movie.select().count() == 1
    assert Actor.select().count() == 1
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 1
    assert Image.select().count() == 2


def test_upsert_movie_rolls_back_database_records_when_cover_image_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123", plot_images=[])
    detail.actors[0].avatar_url = None

    def _cover_broken_downloader(url: str, target_path: Path) -> None:
        if url.endswith("cover.jpg"):
            raise ImageDownloadError("cover network down")
        _fake_downloader(url, target_path)

    service = CatalogImportService(image_downloader=_cover_broken_downloader)

    with pytest.raises(ImageDownloadError):
        service.upsert_movie_from_javdb_detail(detail)

    assert Movie.select().count() == 0
    assert Actor.select().count() == 0
    assert Tag.select().count() == 0
    assert MovieActor.select().count() == 0
    assert MovieTag.select().count() == 0
    assert MoviePlotImage.select().count() == 0
    assert Image.select().count() == 0


def test_upsert_movie_skips_actor_avatar_when_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    detail = _build_detail("ABP-123", plot_images=[])
    detail.cover_image = None

    def _actor_broken_downloader(url: str, target_path: Path) -> None:
        if url.endswith("actor-a.jpg"):
            raise ImageDownloadError("actor network down")
        _fake_downloader(url, target_path)

    service = CatalogImportService(image_downloader=_actor_broken_downloader)
    movie = service.upsert_movie_from_javdb_detail(detail)
    actor = Actor.get(Actor.javdb_id == "actor-ABP-123")

    assert movie.movie_number == "ABP-123"
    assert actor.profile_image is None
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


def test_refresh_movie_metadata_strict_rebuilds_relations_and_replaces_images(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    image_root = tmp_path / "images"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    thin_cover = Image.create(
        origin="movies/ABP-123/thin-cover.jpg",
        small="movies/ABP-123/thin-cover.jpg",
        medium="movies/ABP-123/thin-cover.jpg",
        large="movies/ABP-123/thin-cover.jpg",
    )
    old_cover = Image.create(
        origin="movies/ABP-123/cover.jpg",
        small="movies/ABP-123/cover.jpg",
        medium="movies/ABP-123/cover.jpg",
        large="movies/ABP-123/cover.jpg",
    )
    old_plot = Image.create(
        origin="movies/ABP-123/plots/0.jpg",
        small="movies/ABP-123/plots/0.jpg",
        medium="movies/ABP-123/plots/0.jpg",
        large="movies/ABP-123/plots/0.jpg",
    )
    kept_actor_image = Image.create(
        origin="actors/actor-keep.jpg",
        small="actors/actor-keep.jpg",
        medium="actors/actor-keep.jpg",
        large="actors/actor-keep.jpg",
    )
    removed_actor_image = Image.create(
        origin="actors/actor-remove.jpg",
        small="actors/actor-remove.jpg",
        medium="actors/actor-remove.jpg",
        large="actors/actor-remove.jpg",
    )

    _write_image_file(image_root, old_cover.origin, b"old-cover")
    _write_image_file(image_root, old_plot.origin, b"old-plot")
    _write_image_file(image_root, kept_actor_image.origin, b"old-keep-actor")
    _write_image_file(image_root, removed_actor_image.origin, b"old-remove-actor")
    _write_image_file(image_root, thin_cover.origin, b"thin-cover")

    movie = Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        cover_image=old_cover,
        thin_cover_image=thin_cover,
        summary="old-summary",
        desc="keep-desc",
        desc_zh="keep-desc-zh",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 8, 9, 0, 0),
        is_collection=True,
        is_collection_overridden=True,
        heat=99,
    )
    kept_actor = Actor.create(
        javdb_id="actor-keep",
        name="old-keep",
        alias_name="legacy-keep",
        profile_image=kept_actor_image,
        is_subscribed=True,
    )
    removed_actor = Actor.create(
        javdb_id="actor-remove",
        name="old-remove",
        alias_name="legacy-remove",
        profile_image=removed_actor_image,
    )
    old_tag = Tag.create(name="旧标签")
    MovieActor.create(movie=movie, actor=kept_actor)
    MovieActor.create(movie=movie, actor=removed_actor)
    MovieTag.create(movie=movie, tag=old_tag)
    MoviePlotImage.create(movie=movie, image=old_plot)

    detail = JavdbMovieDetailResource(
        javdb_id="javdb-ABP-123-remote",
        movie_number="ABP-123",
        title="new-title",
        cover_image="https://example.com/new-cover.png",
        release_date="2024-05-20",
        duration_minutes=150,
        score=4.9,
        watched_count=21,
        want_watch_count=22,
        comment_count=23,
        score_number=24,
        is_subscribed=False,
        summary="new-summary",
        series_name="new-series",
        maker_name="新厂商",
        director_name="新导演",
        extra={"remote": "payload"},
        actors=[
            JavdbMovieActorResource(
                javdb_id="actor-keep",
                name="new-keep",
                alias_names=["new-keep", "别名A"],
                avatar_url="https://example.com/new-actor-keep.png",
                gender=2,
            ),
            JavdbMovieActorResource(
                javdb_id="actor-new",
                name="new-actor",
                alias_names=["new-actor"],
                avatar_url=None,
                gender=1,
            ),
        ],
        tags=[
            JavdbMovieTagResource(javdb_id="tag-new-1", name="剧情"),
            JavdbMovieTagResource(javdb_id="tag-new-2", name="无码"),
        ],
        plot_images=["https://example.com/new-plot-0.png"],
    )
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/new-cover.png": _build_image_bytes(900, 600),
                "https://example.com/new-plot-0.png": _build_image_bytes(400, 640),
                "https://example.com/new-actor-keep.png": _build_image_bytes(240, 320),
            }
        )
    )

    def _fake_split(image_path: Path, output_image_path: Path, center_range: int = 100) -> bool:
        output_image_path.parent.mkdir(parents=True, exist_ok=True)
        output_image_path.write_bytes(_build_image_bytes(420, 640))
        return True

    monkeypatch.setattr(service, "_split_image", _fake_split)

    refreshed = service.refresh_movie_metadata_strict(movie, detail)

    refreshed_movie = Movie.get_by_id(refreshed.id)
    refreshed_actor = Actor.get(Actor.javdb_id == "actor-keep")
    created_actor = Actor.get(Actor.javdb_id == "actor-new")
    untouched_removed_actor = Actor.get(Actor.javdb_id == "actor-remove")
    refreshed_plot_links = list(
        MoviePlotImage.select(MoviePlotImage, Image).join(Image).where(MoviePlotImage.movie == refreshed_movie)
    )

    assert refreshed_movie.title == "new-title"
    assert refreshed_movie.summary == "new-summary"
    assert refreshed_movie.series_name == "new-series"
    assert refreshed_movie.series_id is not None
    assert refreshed_movie.maker_name == "新厂商"
    assert refreshed_movie.director_name == "新导演"
    assert refreshed_movie.release_date == datetime(2024, 5, 20)
    assert refreshed_movie.duration_minutes == 150
    assert refreshed_movie.score == 4.9
    assert refreshed_movie.watched_count == 21
    assert refreshed_movie.want_watch_count == 22
    assert refreshed_movie.comment_count == 23
    assert refreshed_movie.score_number == 24
    assert refreshed_movie.extra == {"remote": "payload"}
    assert refreshed_movie.desc == "keep-desc"
    assert refreshed_movie.desc_zh == "keep-desc-zh"
    assert refreshed_movie.thin_cover_image is not None
    assert refreshed_movie.thin_cover_image.origin == "movies/ABP-123/thin-cover.png"
    assert refreshed_movie.movie_number == "ABP-123"
    assert refreshed_movie.javdb_id == "javdb-ABP-123-remote"
    assert refreshed_movie.is_subscribed is True
    assert refreshed_movie.subscribed_at == datetime(2026, 3, 8, 9, 0, 0)
    assert refreshed_movie.is_collection is True
    assert refreshed_movie.is_collection_overridden is True
    assert refreshed_movie.heat == 99
    assert refreshed_movie.cover_image is not None
    assert refreshed_movie.cover_image.origin == "movies/ABP-123/cover.png"
    assert not (image_root / "movies/ABP-123/thin-cover.jpg").exists()
    assert (image_root / "movies/ABP-123/thin-cover.png").exists()
    assert not (image_root / "movies/ABP-123/cover.jpg").exists()

    assert {link.actor.javdb_id for link in MovieActor.select(MovieActor, Actor).join(Actor).where(MovieActor.movie == refreshed_movie)} == {
        "actor-keep",
        "actor-new",
    }
    assert refreshed_actor.name == "new-keep"
    assert refreshed_actor.alias_name == "new-keep / 别名A / legacy-keep"
    assert refreshed_actor.gender == 2
    assert refreshed_actor.profile_image is not None
    assert refreshed_actor.profile_image.origin == "actors/actor-keep.png"
    assert (image_root / "actors/actor-keep.png").exists()
    assert not (image_root / "actors/actor-keep.jpg").exists()
    assert created_actor.profile_image is None
    assert untouched_removed_actor.profile_image_id == removed_actor_image.id
    assert (image_root / "actors/actor-remove.jpg").read_bytes() == b"old-remove-actor"

    assert {
        link.tag.name
        for link in MovieTag.select(MovieTag, Tag).join(Tag).where(MovieTag.movie == refreshed_movie)
    } == {"剧情", "无码"}
    assert refreshed_plot_links[0].image.origin == "movies/ABP-123/plots/0.png"
    assert not (image_root / "movies/ABP-123/plots/0.jpg").exists()


def test_refresh_movie_metadata_strict_falls_back_to_plot_image_when_split_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    image_root = tmp_path / "images"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    old_thin_cover = Image.create(
        origin="movies/ABP-124/thin-cover.jpg",
        small="movies/ABP-124/thin-cover.jpg",
        medium="movies/ABP-124/thin-cover.jpg",
        large="movies/ABP-124/thin-cover.jpg",
    )
    movie = Movie.create(
        javdb_id="javdb-ABP-124",
        movie_number="ABP-124",
        title="old-title",
        thin_cover_image=old_thin_cover,
    )
    _write_image_file(image_root, old_thin_cover.origin, b"old-thin-cover")

    detail = _build_detail(
        "ABP-124",
        plot_images=[
            "https://example.com/plot-1.png",
            "https://example.com/plot-2.png",
        ],
    )
    detail.cover_image = "https://example.com/cover.png"
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.png": _build_image_bytes(900, 600),
                "https://example.com/plot-1.png": _build_image_bytes(800, 600),
                "https://example.com/plot-2.png": _build_image_bytes(420, 680),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        )
    )
    monkeypatch.setattr(service, "_split_image", lambda *_args, **_kwargs: False)

    service.refresh_movie_metadata_strict(movie, detail)

    refreshed_movie = Movie.get_by_id(movie.id)
    plot_links = list(
        MoviePlotImage.select(MoviePlotImage, Image).join(Image).where(MoviePlotImage.movie == refreshed_movie).order_by(MoviePlotImage.id)
    )

    assert refreshed_movie.thin_cover_image_id == plot_links[1].image.id
    assert not (image_root / "movies/ABP-124/thin-cover.jpg").exists()


def test_refresh_movie_metadata_strict_clears_thin_cover_when_split_and_fallback_fail(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    image_root = tmp_path / "images"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    old_thin_cover = Image.create(
        origin="movies/ABP-125/thin-cover.jpg",
        small="movies/ABP-125/thin-cover.jpg",
        medium="movies/ABP-125/thin-cover.jpg",
        large="movies/ABP-125/thin-cover.jpg",
    )
    movie = Movie.create(
        javdb_id="javdb-ABP-125",
        movie_number="ABP-125",
        title="old-title",
        thin_cover_image=old_thin_cover,
    )
    _write_image_file(image_root, old_thin_cover.origin, b"old-thin-cover")

    detail = _build_detail(
        "ABP-125",
        plot_images=[
            "https://example.com/plot-1.png",
            "https://example.com/plot-2.png",
        ],
    )
    detail.cover_image = "https://example.com/cover.png"
    service = CatalogImportService(
        image_downloader=_build_image_downloader(
            {
                "https://example.com/cover.png": _build_image_bytes(900, 600),
                "https://example.com/plot-1.png": _build_image_bytes(800, 600),
                "https://example.com/plot-2.png": _build_image_bytes(820, 600),
                "https://example.com/actor-a.jpg": _build_image_bytes(240, 320),
            }
        )
    )
    monkeypatch.setattr(service, "_split_image", lambda *_args, **_kwargs: False)

    service.refresh_movie_metadata_strict(movie, detail)

    refreshed_movie = Movie.get_by_id(movie.id)

    assert refreshed_movie.thin_cover_image is None
    assert not (image_root / "movies/ABP-125/thin-cover.jpg").exists()


def test_refresh_movie_metadata_strict_clears_missing_cover_plot_and_actor_images(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    image_root = tmp_path / "images"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    cover = Image.create(
        origin="movies/ABP-123/cover.jpg",
        small="movies/ABP-123/cover.jpg",
        medium="movies/ABP-123/cover.jpg",
        large="movies/ABP-123/cover.jpg",
    )
    plot = Image.create(
        origin="movies/ABP-123/plots/0.jpg",
        small="movies/ABP-123/plots/0.jpg",
        medium="movies/ABP-123/plots/0.jpg",
        large="movies/ABP-123/plots/0.jpg",
    )
    actor_image = Image.create(
        origin="actors/actor-1.jpg",
        small="actors/actor-1.jpg",
        medium="actors/actor-1.jpg",
        large="actors/actor-1.jpg",
    )
    _write_image_file(image_root, cover.origin, b"cover")
    _write_image_file(image_root, plot.origin, b"plot")
    _write_image_file(image_root, actor_image.origin, b"actor")

    movie = Movie.create(javdb_id="javdb-ABP-123", movie_number="ABP-123", title="old", cover_image=cover)
    actor = Actor.create(javdb_id="actor-1", name="actor", alias_name="", profile_image=actor_image)
    MovieActor.create(movie=movie, actor=actor)
    MoviePlotImage.create(movie=movie, image=plot)
    service = CatalogImportService(image_downloader=_fake_downloader)

    detail = _build_detail("ABP-123", plot_images=[])
    detail.cover_image = None
    detail.actors[0].javdb_id = "actor-1"
    detail.actors[0].avatar_url = None
    detail.tags = []

    service.refresh_movie_metadata_strict(movie, detail)

    refreshed_movie = Movie.get_by_id(movie.id)
    refreshed_actor = Actor.get_by_id(actor.id)

    assert refreshed_movie.cover_image is None
    assert refreshed_actor.profile_image is None
    assert MoviePlotImage.select().count() == 0
    assert not (image_root / "movies/ABP-123/cover.jpg").exists()
    assert not (image_root / "movies/ABP-123/plots/0.jpg").exists()
    assert not (image_root / "actors/actor-1.jpg").exists()


def test_refresh_movie_metadata_strict_keeps_existing_state_when_new_image_download_fails(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    image_root = tmp_path / "images"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    cover = Image.create(
        origin="movies/ABP-123/cover.jpg",
        small="movies/ABP-123/cover.jpg",
        medium="movies/ABP-123/cover.jpg",
        large="movies/ABP-123/cover.jpg",
    )
    plot = Image.create(
        origin="movies/ABP-123/plots/0.jpg",
        small="movies/ABP-123/plots/0.jpg",
        medium="movies/ABP-123/plots/0.jpg",
        large="movies/ABP-123/plots/0.jpg",
    )
    actor_image = Image.create(
        origin="actors/actor-1.jpg",
        small="actors/actor-1.jpg",
        medium="actors/actor-1.jpg",
        large="actors/actor-1.jpg",
    )
    _write_image_file(image_root, cover.origin, b"old-cover")
    _write_image_file(image_root, plot.origin, b"old-plot")
    _write_image_file(image_root, actor_image.origin, b"old-actor")

    movie = Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        cover_image=cover,
        summary="old-summary",
        desc="keep-desc",
    )
    actor = Actor.create(javdb_id="actor-1", name="old-actor", alias_name="", profile_image=actor_image)
    tag = Tag.create(name="旧标签")
    MovieActor.create(movie=movie, actor=actor)
    MovieTag.create(movie=movie, tag=tag)
    MoviePlotImage.create(movie=movie, image=plot)

    def _broken_downloader(url: str, target_path: Path) -> None:
        if url.endswith("new-plot.png"):
            raise ImageDownloadError("plot failed")
        _fake_downloader(url, target_path)

    detail = _build_detail("ABP-123", plot_images=["https://example.com/new-plot.png"])
    detail.cover_image = "https://example.com/new-cover.png"
    detail.actors[0].javdb_id = "actor-1"
    detail.actors[0].avatar_url = "https://example.com/new-actor.png"
    service = CatalogImportService(image_downloader=_broken_downloader)

    with pytest.raises(ImageDownloadError):
        service.refresh_movie_metadata_strict(movie, detail)

    refreshed_movie = Movie.get_by_id(movie.id)
    refreshed_actor = Actor.get_by_id(actor.id)

    assert refreshed_movie.title == "old-title"
    assert refreshed_movie.summary == "old-summary"
    assert refreshed_movie.cover_image_id == cover.id
    assert refreshed_movie.desc == "keep-desc"
    assert refreshed_actor.profile_image_id == actor_image.id
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 1
    assert (image_root / "movies/ABP-123/cover.jpg").read_bytes() == b"old-cover"
    assert (image_root / "movies/ABP-123/plots/0.jpg").read_bytes() == b"old-plot"
    assert (image_root / "actors/actor-1.jpg").read_bytes() == b"old-actor"
    assert not (image_root / "movies/ABP-123/cover.png").exists()
