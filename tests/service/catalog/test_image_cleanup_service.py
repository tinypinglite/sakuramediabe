import zipfile

from src.common.media_paths import (
    MOVIE_ASSETS_PACK_NAME,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie, MoviePlotImage
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


def _prepare_movie_with_pack(tmp_path, monkeypatch):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    movie = Movie.create(movie_number="CLEAN-MOV-001", title="cleanup movie")
    movie_dir = image_root / movie_asset_relative_dir(
        normalize_asset_dir_name(movie.movie_number)
    )
    movie_dir.mkdir(parents=True)
    origins: list[str] = []
    images: list[Image] = []
    for name in ("cover.png", "plot-0.png"):
        origin = (movie_dir / name).relative_to(image_root).as_posix()
        origins.append(origin)
        images.append(Image.create(origin=origin))
    movie.cover_image = images[0]
    movie.save(only=[Movie.cover_image])
    MoviePlotImage.create(movie=movie, image=images[1])
    pack_path = movie_dir / MOVIE_ASSETS_PACK_NAME
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("cover.png", b"cover-bytes")
        archive.writestr("plot-0.png", b"plot-bytes")
    return origins, images, pack_path


def test_cleanup_removes_movie_pack_when_all_images_unreferenced(
    test_db, monkeypatch, tmp_path
):
    origins, images, pack_path = _prepare_movie_with_pack(tmp_path, monkeypatch)
    MoviePlotImage.delete().execute()
    Movie.update(cover_image=None).execute()
    for image in images:
        image.delete_instance()

    ImageCleanupService.delete_obsolete_image_files(set(origins))

    assert not pack_path.exists()


def test_cleanup_rebuilds_movie_pack_keeping_referenced_entries(
    test_db, monkeypatch, tmp_path
):
    origins, images, pack_path = _prepare_movie_with_pack(tmp_path, monkeypatch)
    # 保留 cover（模拟仍被引用），删除剧情图记录。
    MoviePlotImage.delete().execute()
    images[1].delete_instance()

    ImageCleanupService.delete_obsolete_image_files({origins[1]})

    assert pack_path.is_file()
    with zipfile.ZipFile(pack_path) as archive:
        assert archive.namelist() == ["cover.png"]
        assert archive.read("cover.png") == b"cover-bytes"
