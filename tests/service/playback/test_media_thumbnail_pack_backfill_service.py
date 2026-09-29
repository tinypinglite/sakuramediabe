import zipfile
from types import SimpleNamespace

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.service.playback.media_thumbnail_pack_backfill_service import (
    MediaThumbnailPackBackfillService,
)
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService


def _reporter():
    events: list[dict] = []
    return SimpleNamespace(emit=lambda **payload: events.append(payload)), events


def _prepare_media(
    tmp_path,
    monkeypatch,
    *,
    offsets: tuple[int, ...] = (10, 20),
    with_files: tuple[int, ...] | None = None,
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    library = MediaLibrary.create(
        name="pack-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="PACK-001", title="pack")
    media = Media.create(movie=movie, library=library, file_name="pack.mp4")
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    thumbnails_dir.mkdir(parents=True)
    origins = []
    for offset in offsets:
        relative_path = (
            (thumbnails_dir / f"{offset}.webp").relative_to(image_root).as_posix()
        )
        origins.append(relative_path)
        if with_files is None or offset in with_files:
            (thumbnails_dir / f"{offset}.webp").write_bytes(f"thumb-{offset}".encode())
        image = Image.create(origin=relative_path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media, thumbnails_dir, origins


def test_backfill_packs_legacy_thumbnails_and_is_idempotent(
    test_db, monkeypatch, tmp_path
):
    _, thumbnails_dir, _ = _prepare_media(tmp_path, monkeypatch)
    reporter, events = _reporter()

    stats = MediaThumbnailPackBackfillService.backfill(reporter=reporter)

    assert stats["candidate_media"] == 1
    assert stats["packed_media"] == 1
    assert stats["failed_media"] == 0
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    assert pack_path.is_file()
    with zipfile.ZipFile(pack_path) as archive:
        assert sorted(archive.namelist()) == ["10.webp", "20.webp"]
        assert archive.read("10.webp") == b"thumb-10"
    assert not thumbnails_dir.exists()
    assert events[-1]["current"] == events[-1]["total"] == 1
    assert events[-1]["summary_patch"] == stats

    second = MediaThumbnailPackBackfillService.backfill(reporter=reporter)
    assert second["packed_media"] == 0
    assert second["already_packed_media"] == 1


def test_backfill_skips_media_with_missing_legacy_file(test_db, monkeypatch, tmp_path):
    _, thumbnails_dir, _ = _prepare_media(tmp_path, monkeypatch, with_files=(10,))
    reporter, _ = _reporter()

    stats = MediaThumbnailPackBackfillService.backfill(reporter=reporter)

    assert stats["skipped_missing_files"] == 1
    assert stats["packed_media"] == 0
    assert not thumbnails_dir.with_name("thumbnails.zip").exists()
    assert (thumbnails_dir / "10.webp").is_file()


def test_backfill_cleans_residue_when_pack_already_covers_rows(
    test_db, monkeypatch, tmp_path
):
    _, thumbnails_dir, _ = _prepare_media(tmp_path, monkeypatch)
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        for offset in (10, 20):
            archive.writestr(f"{offset}.webp", f"thumb-{offset}".encode())
    reporter, _ = _reporter()

    stats = MediaThumbnailPackBackfillService.backfill(reporter=reporter)

    assert stats["cleaned_media"] == 1
    assert stats["packed_media"] == 0
    assert pack_path.is_file()
    assert not thumbnails_dir.exists()


def test_backfill_keeps_legacy_files_when_pack_incomplete(
    test_db, monkeypatch, tmp_path
):
    _, thumbnails_dir, _ = _prepare_media(tmp_path, monkeypatch)
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("10.webp", b"thumb-10")
    reporter, _ = _reporter()

    stats = MediaThumbnailPackBackfillService.backfill(reporter=reporter)

    assert stats["incomplete_media"] == 1
    assert (thumbnails_dir / "10.webp").is_file()
    assert (thumbnails_dir / "20.webp").is_file()


def test_unpack_restores_files_and_removes_pack(test_db, monkeypatch, tmp_path):
    media, thumbnails_dir, _ = _prepare_media(tmp_path, monkeypatch)
    reporter, _ = _reporter()
    MediaThumbnailPackBackfillService.backfill(reporter=reporter)
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    assert pack_path.is_file()

    stats = MediaThumbnailPackBackfillService.unpack(media_id=media.id)

    assert stats["unpacked_media"] == 1
    assert stats["failed_media"] == 0
    assert not pack_path.exists()
    assert (thumbnails_dir / "10.webp").read_bytes() == b"thumb-10"
    assert (thumbnails_dir / "20.webp").read_bytes() == b"thumb-20"
