import zipfile

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie
from src.plugins.provider_protocol import ThumbnailArtifact
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService


def test_persist_reuses_existing_image_row_for_same_origin(
    test_db, monkeypatch, tmp_path
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    library = MediaLibrary.create(
        name="persist-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="PERSIST-001", title="persist")
    media = Media.create(movie=movie, library=library, file_name="persist.mp4")
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    relative_path = (thumbnails_dir / "10.webp").relative_to(image_root).as_posix()
    # 模拟迁移/清理容错后残留的共享引用：同 origin 的 Image 行已存在。
    existing = Image.create(origin=relative_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_file = workspace / "10.webp"
    artifact_file.write_bytes(b"new-10")

    count = ThumbnailArtifactService.persist(
        media,
        [
            (
                ThumbnailArtifact(offset_seconds=10, relative_path="10.webp"),
                artifact_file,
            )
        ],
    )

    assert count == 1
    thumbnail = MediaThumbnail.get(MediaThumbnail.media == media)
    assert thumbnail.image_id == existing.id
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)
    with zipfile.ZipFile(pack_path) as archive:
        assert archive.read("10.webp") == b"new-10"
