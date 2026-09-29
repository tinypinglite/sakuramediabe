import zipfile

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService


def _prepare_packed_media(tmp_path, monkeypatch):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    library = MediaLibrary.create(
        name="cleanup-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="CLEAN-001", title="cleanup")
    media = Media.create(movie=movie, library=library, file_name="cleanup.mp4")
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    thumbnails_dir.mkdir(parents=True)
    origins: list[str] = []
    images: list[Image] = []
    thumbnails: list[MediaThumbnail] = []
    for offset in (10, 20):
        relative_path = (
            (thumbnails_dir / f"{offset}.webp").relative_to(image_root).as_posix()
        )
        origins.append(relative_path)
        image = Image.create(origin=relative_path)
        images.append(image)
        thumbnails.append(
            MediaThumbnail.create(media=media, image=image, offset=offset)
        )
    pack_path = thumbnails_dir.with_name("thumbnails.zip")
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("10.webp", b"thumb-10")
        archive.writestr("20.webp", b"thumb-20")
    thumbnails_dir.rmdir()
    return origins, images, thumbnails, pack_path


def test_cleanup_removes_pack_when_all_images_unreferenced(
    test_db, monkeypatch, tmp_path
):
    origins, images, thumbnails, pack_path = _prepare_packed_media(
        tmp_path, monkeypatch
    )
    for thumbnail in thumbnails:
        thumbnail.delete_instance()
    for image in images:
        image.delete_instance()

    ImageCleanupService.delete_obsolete_image_files(set(origins))

    assert not pack_path.exists()


def test_cleanup_rebuilds_pack_keeping_referenced_entries(
    test_db, monkeypatch, tmp_path
):
    origins, images, thumbnails, pack_path = _prepare_packed_media(
        tmp_path, monkeypatch
    )
    # 保留 offset=10（模拟被时刻钉住），移除 offset=20。
    thumbnails[1].delete_instance()
    images[1].delete_instance()

    ImageCleanupService.delete_obsolete_image_files({origins[1]})

    assert pack_path.is_file()
    with zipfile.ZipFile(pack_path) as archive:
        assert archive.namelist() == ["10.webp"]
        assert archive.read("10.webp") == b"thumb-10"


def test_cleanup_unlinks_plain_files_without_pack(test_db, monkeypatch, tmp_path):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    relative_path = "movies/aa/PLAIN-001/cover.jpg"
    target = image_root / relative_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"cover-bytes")

    ImageCleanupService.delete_obsolete_image_files({relative_path})

    assert not target.exists()
