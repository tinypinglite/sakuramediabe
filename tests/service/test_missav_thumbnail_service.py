from pathlib import Path

import pytest
from PIL import Image as PillowImage

import src.service.catalog.missav_thumbnail_service as missav_service_module
from src.config.config import settings
from sakuramedia_metadata_providers.models import MissavThumbnailManifest
from src.service.catalog.missav_thumbnail_service import MissavThumbnailService


def _build_manifest(
    movie_number: str,
    *,
    pic_num: int,
    width: int,
    height: int,
    col: int,
    row: int,
    urls: list[str],
    offset_x: int = 0,
    offset_y: int = 0,
) -> MissavThumbnailManifest:
    return MissavThumbnailManifest(
        movie_number=movie_number,
        page_url=f"https://missav.ws/cn/{movie_number}",
        pic_num=pic_num,
        width=width,
        height=height,
        col=col,
        row=row,
        offset_x=offset_x,
        offset_y=offset_y,
        urls=urls,
    )


def _write_sprite(
    target_path: Path,
    *,
    width: int,
    height: int,
    row: int,
    col: int,
    colors: list[tuple[int, int, int]],
) -> None:
    sprite = PillowImage.new("RGB", (width * row, height * col))
    color_index = 0
    for y_index in range(col):
        for x_index in range(row):
            color = colors[color_index]
            color_index += 1
            cell = PillowImage.new("RGB", (width, height), color)
            sprite.paste(cell, (x_index * width, y_index * height))
    sprite.save(target_path, format="JPEG")


def test_get_movie_thumbnails_downloads_and_slices_sprites(
    tmp_path,
    monkeypatch,
    build_signed_image_url,
):
    manifest = _build_manifest(
        "SSNI-888",
        pic_num=4,
        width=20,
        height=10,
        col=2,
        row=2,
        urls=["https://cdn.example.com/seek/_0.jpg"],
    )

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def fetch_thumbnail_manifest(self, movie_number: str):
            self.calls.append(movie_number)
            return manifest

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=20,
            height=10,
            row=2,
            col=2,
            colors=[
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
            ],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    provider = FakeProvider()
    service = MissavThumbnailService(provider=provider, sprite_downloader=fake_downloader)

    resource = service.get_movie_thumbnails("SSNI-888")

    assert provider.calls == ["SSNI-888"]
    assert resource.model_dump() == {
        "movie_number": "SSNI-888",
        "source": "missav",
        "total": 4,
        "items": [
            {
                "index": 0,
                "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/0.jpg"),
            },
            {
                "index": 1,
                "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/1.jpg"),
            },
            {
                "index": 2,
                "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/2.jpg"),
            },
            {
                "index": 3,
                "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/3.jpg"),
            },
        ],
    }
    first_frame = tmp_path / "movies" / "SSNI-888" / "missav-seek" / "frames" / "0.jpg"
    assert first_frame.exists()
    with PillowImage.open(first_frame) as image:
        assert image.size == (20, 10)


def test_get_movie_thumbnails_reuses_cache_until_refresh(
    tmp_path,
    monkeypatch,
):
    manifests = [
        _build_manifest(
            "SSNI-888",
            pic_num=4,
            width=10,
            height=10,
            col=2,
            row=2,
            urls=["https://cdn.example.com/seek/_0.jpg"],
        ),
        _build_manifest(
            "SSNI-888",
            pic_num=2,
            width=10,
            height=10,
            col=1,
            row=2,
            urls=["https://cdn.example.com/seek/_0.jpg"],
        ),
    ]

    class FakeProvider:
        def __init__(self):
            self.calls = []

        def fetch_thumbnail_manifest(self, movie_number: str):
            self.calls.append(movie_number)
            return manifests[len(self.calls) - 1]

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=10,
            height=10,
            row=2,
            col=2,
            colors=[
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
            ],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    provider = FakeProvider()
    service = MissavThumbnailService(provider=provider, sprite_downloader=fake_downloader)

    first = service.get_movie_thumbnails("SSNI-888")
    second = service.get_movie_thumbnails("SSNI-888")
    third = service.get_movie_thumbnails("SSNI-888", refresh=True)

    assert first.total == 4
    assert second.total == 4
    assert third.total == 2
    assert provider.calls == ["SSNI-888", "SSNI-888"]


def test_get_movie_thumbnails_reports_progress_events_when_cache_is_rebuilt(
    tmp_path,
    monkeypatch,
):
    manifest = _build_manifest(
        "SSNI-888",
        pic_num=4,
        width=10,
        height=10,
        col=2,
        row=2,
        urls=["https://cdn.example.com/seek/_0.jpg"],
    )
    progress_events = []

    class FakeProvider:
        def fetch_thumbnail_manifest(self, movie_number: str):
            return manifest

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=10,
            height=10,
            row=2,
            col=2,
            colors=[
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
            ],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    service = MissavThumbnailService(provider=FakeProvider(), sprite_downloader=fake_downloader)

    resource = service.get_movie_thumbnails(
        "SSNI-888",
        progress_callback=lambda event, payload: progress_events.append((event, payload)),
    )

    assert resource.total == 4
    assert progress_events == [
        (
            "manifest_resolved",
            {"movie_number": "SSNI-888", "sprite_total": 1, "thumbnail_total": 4},
        ),
        ("download_started", {"total": 1}),
        ("download_progress", {"completed": 1, "total": 1}),
        ("download_finished", {"completed": 1, "total": 1}),
        ("slice_started", {"total": 4}),
        ("slice_progress", {"completed": 4, "total": 4}),
        ("slice_finished", {"completed": 4, "total": 4}),
    ]


def test_get_movie_thumbnails_skips_progress_events_when_cache_hits(
    tmp_path,
    monkeypatch,
):
    manifest = _build_manifest(
        "SSNI-888",
        pic_num=4,
        width=10,
        height=10,
        col=2,
        row=2,
        urls=["https://cdn.example.com/seek/_0.jpg"],
    )

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def fetch_thumbnail_manifest(self, movie_number: str):
            self.calls += 1
            return manifest

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=10,
            height=10,
            row=2,
            col=2,
            colors=[
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
            ],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    provider = FakeProvider()
    service = MissavThumbnailService(provider=provider, sprite_downloader=fake_downloader)
    service.get_movie_thumbnails("SSNI-888")

    progress_events = []
    resource = service.get_movie_thumbnails(
        "SSNI-888",
        progress_callback=lambda event, payload: progress_events.append((event, payload)),
    )

    assert resource.total == 4
    assert provider.calls == 1
    assert progress_events == []


def test_get_movie_thumbnails_stops_at_pic_num_boundary(
    tmp_path,
    monkeypatch,
):
    manifest = _build_manifest(
        "SSNI-888",
        pic_num=5,
        width=12,
        height=8,
        col=2,
        row=2,
        urls=[
            "https://cdn.example.com/seek/_0.jpg",
            "https://cdn.example.com/seek/_1.jpg",
        ],
    )

    class FakeProvider:
        def fetch_thumbnail_manifest(self, movie_number: str):
            return manifest

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=12,
            height=8,
            row=2,
            col=2,
            colors=[
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
            ],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    service = MissavThumbnailService(provider=FakeProvider(), sprite_downloader=fake_downloader)

    resource = service.get_movie_thumbnails("SSNI-888")

    assert resource.total == 5
    assert (tmp_path / "movies" / "SSNI-888" / "missav-seek" / "frames" / "4.jpg").exists()
    assert not (tmp_path / "movies" / "SSNI-888" / "missav-seek" / "frames" / "5.jpg").exists()


def test_get_movie_thumbnails_uses_bounded_download_workers(
    tmp_path,
    monkeypatch,
):
    manifest = _build_manifest(
        "SSNI-888",
        pic_num=6,
        width=4,
        height=4,
        col=1,
        row=1,
        urls=[
            "https://cdn.example.com/seek/_0.jpg",
            "https://cdn.example.com/seek/_1.jpg",
            "https://cdn.example.com/seek/_2.jpg",
            "https://cdn.example.com/seek/_3.jpg",
            "https://cdn.example.com/seek/_4.jpg",
            "https://cdn.example.com/seek/_5.jpg",
        ],
    )
    captured_workers = []

    class FakeProvider:
        def fetch_thumbnail_manifest(self, movie_number: str):
            return manifest

    class FakeFuture:
        def __init__(self, func, *args):
            self._exception = None
            try:
                self._result = func(*args)
            except Exception as exc:
                self._exception = exc

        def result(self):
            if self._exception is not None:
                raise self._exception
            return self._result

    class FakeExecutor:
        def __init__(self, max_workers: int, thread_name_prefix: str):
            captured_workers.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, func, *args):
            return FakeFuture(func, *args)

    def fake_as_completed(futures):
        return list(futures)

    def fake_downloader(sprite_url: str, target_path: Path, page_url: str):
        _write_sprite(
            target_path,
            width=4,
            height=4,
            row=1,
            col=1,
            colors=[(255, 0, 0)],
        )

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path))
    monkeypatch.setattr(missav_service_module, "ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr(missav_service_module, "as_completed", fake_as_completed)

    service = MissavThumbnailService(provider=FakeProvider(), sprite_downloader=fake_downloader)
    resource = service.get_movie_thumbnails("SSNI-888")

    assert resource.total == 6
    assert captured_workers == [4]
