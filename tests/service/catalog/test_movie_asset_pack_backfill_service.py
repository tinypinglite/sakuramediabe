import zipfile
from types import SimpleNamespace

from src.common.image_store import read_image_bytes
from src.common.media_paths import (
    MOVIE_ASSETS_PACK_NAME,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie, MoviePlotImage
from src.service.catalog.movie_asset_pack_backfill_service import (
    MovieAssetPackBackfillService,
)
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService


def _reporter():
    events: list[dict] = []
    return SimpleNamespace(emit=lambda **payload: events.append(payload)), events


def _prepare_movie(
    tmp_path,
    monkeypatch,
    *,
    with_files: set[str] | None = None,
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    movie = Movie.create(movie_number="PACK-001", title="pack")
    movie_dir = image_root / movie_asset_relative_dir(
        normalize_asset_dir_name(movie.movie_number)
    )
    movie_dir.mkdir(parents=True)
    origins: list[str] = []

    def _add(name: str, data: bytes) -> Image:
        origin = (movie_dir / name).relative_to(image_root).as_posix()
        origins.append(origin)
        if with_files is None or name in with_files:
            (movie_dir / name).write_bytes(data)
        return Image.create(origin=origin)

    cover = _add("cover.png", b"cover-bytes")
    movie.cover_image = cover
    movie.save(only=[Movie.cover_image])
    for index in range(2):
        image = _add(f"plot-{index}.png", f"plot-{index}".encode())
        MoviePlotImage.create(movie=movie, image=image)
    return movie, movie_dir, origins


def test_backfill_packs_movie_assets_and_is_idempotent(test_db, monkeypatch, tmp_path):
    _, movie_dir, origins = _prepare_movie(tmp_path, monkeypatch)
    reporter, events = _reporter()

    stats = MovieAssetPackBackfillService.backfill(reporter=reporter)

    assert stats["candidate_movies"] == 1
    assert stats["packed_movies"] == 1
    assert stats["failed_movies"] == 0
    pack_path = movie_dir / MOVIE_ASSETS_PACK_NAME
    with zipfile.ZipFile(pack_path) as archive:
        assert sorted(archive.namelist()) == [
            "cover.png",
            "plot-0.png",
            "plot-1.png",
        ]
        assert archive.read("cover.png") == b"cover-bytes"
    assert [p.name for p in movie_dir.iterdir()] == [MOVIE_ASSETS_PACK_NAME]
    assert read_image_bytes(origins[0]) == b"cover-bytes"
    assert events[-1]["summary_patch"] == stats

    second = MovieAssetPackBackfillService.backfill(reporter=reporter)
    assert second["packed_movies"] == 0
    assert second["already_packed_movies"] == 1


def test_backfill_skips_movie_with_missing_legacy_file(test_db, monkeypatch, tmp_path):
    _, movie_dir, _ = _prepare_movie(tmp_path, monkeypatch, with_files={"cover.png"})
    reporter, _ = _reporter()

    stats = MovieAssetPackBackfillService.backfill(reporter=reporter)

    assert stats["skipped_missing_files"] == 1
    assert stats["packed_movies"] == 0
    assert not (movie_dir / MOVIE_ASSETS_PACK_NAME).exists()
    assert (movie_dir / "cover.png").is_file()


def test_backfill_cleans_residue_when_pack_exists(test_db, monkeypatch, tmp_path):
    _, movie_dir, _ = _prepare_movie(tmp_path, monkeypatch)
    with zipfile.ZipFile(
        movie_dir / MOVIE_ASSETS_PACK_NAME, "w", zipfile.ZIP_STORED
    ) as archive:
        archive.writestr("cover.png", b"cover-bytes")
        archive.writestr("plot-0.png", b"plot-0")
        archive.writestr("plot-1.png", b"plot-1")
    reporter, _ = _reporter()

    stats = MovieAssetPackBackfillService.backfill(reporter=reporter)

    assert stats["cleaned_movies"] == 1
    assert stats["packed_movies"] == 0
    assert [p.name for p in movie_dir.iterdir()] == [MOVIE_ASSETS_PACK_NAME]


def test_backfill_packs_movie_with_packed_timeline_thumbnails(
    test_db, monkeypatch, tmp_path
):
    movie, movie_dir, _ = _prepare_movie(tmp_path, monkeypatch)
    library = MediaLibrary.create(
        name="pack-media", provider_key="demo", provider_config={}
    )
    media = Media.create(movie=movie, library=library, file_name="pack.mp4")
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    thumbnails_dir.mkdir(parents=True)
    thumbnail_origin = (
        (thumbnails_dir / "10.webp").relative_to(tmp_path / "assets").as_posix()
    )
    thumbnail_image = Image.create(origin=thumbnail_origin)
    MediaThumbnail.create(media=media, image=thumbnail_image, offset=10)
    with zipfile.ZipFile(
        thumbnails_dir.with_name("thumbnails.zip"), "w", zipfile.ZIP_STORED
    ) as archive:
        archive.writestr("10.webp", b"thumb-10")
    thumbnails_dir.rmdir()

    reporter, _ = _reporter()
    stats = MovieAssetPackBackfillService.backfill(reporter=reporter)

    assert stats["packed_movies"] == 1
    assert stats["skipped_missing_files"] == 0
    with zipfile.ZipFile(movie_dir / MOVIE_ASSETS_PACK_NAME) as archive:
        assert sorted(archive.namelist()) == ["cover.png", "plot-0.png", "plot-1.png"]
    assert read_image_bytes(thumbnail_origin) == b"thumb-10"
