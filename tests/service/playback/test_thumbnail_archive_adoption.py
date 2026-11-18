from io import BytesIO

from PIL import Image as PILImage

from src.config.config import settings
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaThumbnail,
    MediaThumbnailArchive,
    Movie,
    VideoItem,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ImportFile,
    ProviderUnavailableError,
    StagedMedia,
    ThumbnailArtifact,
    ThumbnailGeneration,
)
from src.service.playback import media_service
from src.service.playback.media_service import MediaService
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService
from src.service.transfers.imports.import_service import MediaImportService

FILE_HASH = "a" * 64


def _webp_bytes() -> bytes:
    buffer = BytesIO()
    PILImage.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="WEBP")
    return buffer.getvalue()


def _library(name: str = "archive-library") -> MediaLibrary:
    return MediaLibrary.create(name=name, provider_key="test", provider_config={})


def _movie(number: str) -> Movie:
    return Movie.create(movie_number=number, title=number)


def _media(
    library: MediaLibrary,
    *,
    file_hash: str | None = FILE_HASH,
    valid: bool = True,
    movie: Movie | None = None,
    video_item: VideoItem | None = None,
    file_name: str = "archive.mp4",
) -> Media:
    return Media.create(
        movie=movie,
        video_item=video_item,
        library=library,
        file_name=file_name,
        file_hash=file_hash,
        valid=valid,
    )


def _persist_thumbnails(media: Media, offsets: tuple[int, ...] = (0, 10)) -> None:
    workspace = ThumbnailArtifactService.thumbnail_directory(media).parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for offset in offsets:
        source = workspace / f"{offset}.webp"
        source.write_bytes(_webp_bytes())
        artifacts.append(
            (ThumbnailArtifact(offset_seconds=offset, relative_path=source.name), source)
        )
    ThumbnailArtifactService.persist(media, artifacts)


def _patch_image_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))


def _patch_qdrant(monkeypatch) -> None:
    monkeypatch.setattr(
        media_service,
        "get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda _self, _media_id: None})(),
    )


class _DeletingStorage:
    def delete_media(self, *, media):
        return None


def test_delete_media_archives_thumbnails_and_keeps_files(
    test_db, monkeypatch, tmp_path
):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    media = _media(library, movie=_movie("ARCH-001"))
    _persist_thumbnails(media)
    image_ids = [
        image_id
        for (image_id,) in MediaThumbnail.select(MediaThumbnail.image_id)
        .where(MediaThumbnail.media == media)
        .order_by(MediaThumbnail.offset)
        .tuples()
    ]
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _DeletingStorage()
    )

    MediaService.delete_media(media.id)

    assert Media.get_or_none(Media.id == media.id) is None
    assert MediaThumbnail.select().where(MediaThumbnail.media == media.id).count() == 0
    archived = list(
        MediaThumbnailArchive.select()
        .where(MediaThumbnailArchive.file_hash == FILE_HASH)
        .order_by(MediaThumbnailArchive.offset)
    )
    assert [row.offset for row in archived] == [0, 10]
    assert [row.image_id for row in archived] == image_ids
    assert all(Image.get_or_none(Image.id == row.image_id) is not None for row in archived)
    assert pack_path.is_file()


def test_delete_media_archive_is_idempotent_for_same_hash(
    test_db, monkeypatch, tmp_path
):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    movie = _movie("ARCH-002")
    first = _media(library, movie=movie, file_name="first.mp4")
    second = _media(library, movie=movie, file_name="second.mp4")
    _persist_thumbnails(first)
    _persist_thumbnails(second)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _DeletingStorage()
    )

    MediaService.delete_media(first.id)
    MediaService.delete_media(second.id)

    archived = list(
        MediaThumbnailArchive.select().where(MediaThumbnailArchive.file_hash == FILE_HASH)
    )
    assert len(archived) == 2
    assert sorted(row.offset for row in archived) == [0, 10]


def test_delete_invalid_media_skips_provider_and_keeps_thumbnails(
    test_db, monkeypatch, tmp_path
):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    media = _media(library, movie=_movie("ARCH-003"), valid=False)
    _persist_thumbnails(media)
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)

    def unavailable(_handle):
        raise ProviderUnavailableError("test")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", unavailable)

    MediaService.delete_media(media.id)

    assert Media.get_or_none(Media.id == media.id) is None
    assert MediaThumbnailArchive.select().count() == 2
    assert pack_path.is_file()


def test_delete_media_without_hash_keeps_legacy_delete(test_db, monkeypatch, tmp_path):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    media = _media(library, movie=_movie("ARCH-004"), file_hash=None)
    _persist_thumbnails(media)
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _DeletingStorage()
    )

    MediaService.delete_media(media.id)

    assert Media.get_or_none(Media.id == media.id) is None
    assert MediaThumbnailArchive.select().count() == 0
    assert Image.select().count() == 0
    assert not pack_path.exists()


def test_thumbnail_task_adopts_same_hash_sibling_without_provider(
    test_db, monkeypatch, tmp_path
):
    _patch_image_root(monkeypatch, tmp_path)
    library = _library()
    movie = _movie("ARCH-005")
    source = _media(library, movie=movie, file_name="source.mp4")
    target = _media(library, movie=movie, file_name="target.mp4")
    _persist_thumbnails(source)
    source_image_ids = [
        image_id
        for (image_id,) in MediaThumbnail.select(MediaThumbnail.image_id)
        .where(MediaThumbnail.media == source)
        .order_by(MediaThumbnail.offset)
        .tuples()
    ]

    def unexpected(_handle):
        raise AssertionError("adoption must not touch provider")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", unexpected)

    outcome = MediaThumbnailTaskService._generate_one_locked(target.id)

    assert outcome.state == "adopted"
    assert outcome.generated_count == 2
    target_thumbnails = list(
        MediaThumbnail.select()
        .where(MediaThumbnail.media == target)
        .order_by(MediaThumbnail.offset)
    )
    assert [row.offset for row in target_thumbnails] == [0, 10]
    assert [row.image_id for row in target_thumbnails] == source_image_ids
    assert Media.get_by_id(target.id).thumbnail_generation_state == (
        Media.THUMBNAIL_STATE_SUCCEEDED
    )


def _import_video(library: MediaLibrary, file_hash: str):
    source_file = ImportFile(
        source_ref={"source": "origin"},
        name="video.mp4",
        relative_path="video.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"target": "video.mp4"},
        receipt={"receipt": "video"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (source_file,)

        def stage_import_file(self, **_kwargs):
            return staged

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt

        def compute_file_hash(self, *, media):
            return file_hash

    return MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "directory"}, library.id, media_kind="video")


def _media_thumbnail_offsets(media: Media) -> list[int]:
    return [
        offset
        for (offset,) in MediaThumbnail.select(MediaThumbnail.offset)
        .where(MediaThumbnail.media == media)
        .order_by(MediaThumbnail.offset)
        .tuples()
    ]


def test_import_adopts_existing_thumbnails_by_hash(test_db, monkeypatch, tmp_path):
    _patch_image_root(monkeypatch, tmp_path)
    library = _library()
    source_video = VideoItem.create(title="source")
    source = _media(library, video_item=source_video, file_name="source.mp4")
    _persist_thumbnails(source)
    source_image_ids = sorted(
        image_id
        for (image_id,) in MediaThumbnail.select(MediaThumbnail.image_id)
        .where(MediaThumbnail.media == source)
        .tuples()
    )

    result = _import_video(library, FILE_HASH)

    assert result.imported_count == 1
    imported = Media.get(Media.id != source.id)
    adopted = list(
        MediaThumbnail.select()
        .where(MediaThumbnail.media == imported)
        .order_by(MediaThumbnail.offset)
    )
    assert [row.offset for row in adopted] == [0, 10]
    assert sorted(row.image_id for row in adopted) == source_image_ids
    assert imported.thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED


def test_import_adopts_archived_thumbnails_after_source_deleted(
    test_db, monkeypatch, tmp_path
):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    source_video = VideoItem.create(title="dead-source")
    source = _media(
        library, video_item=source_video, file_name="source.mp4", valid=False
    )
    _persist_thumbnails(source)
    source_image_ids = sorted(
        image_id
        for (image_id,) in MediaThumbnail.select(MediaThumbnail.image_id)
        .where(MediaThumbnail.media == source)
        .tuples()
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: _DeletingStorage()
    )
    MediaService.delete_media(source.id)

    result = _import_video(library, FILE_HASH)

    assert result.imported_count == 1
    imported = Media.get(Media.id != source.id)
    assert _media_thumbnail_offsets(imported) == [0, 10]
    adopted_image_ids = sorted(
        image_id
        for (image_id,) in MediaThumbnail.select(MediaThumbnail.image_id)
        .where(MediaThumbnail.media == imported)
        .tuples()
    )
    assert adopted_image_ids == source_image_ids
    assert imported.thumbnail_generation_state == Media.THUMBNAIL_STATE_SUCCEEDED


def test_force_reset_regenerates_instead_of_adopting(test_db, monkeypatch, tmp_path):
    _patch_image_root(monkeypatch, tmp_path)
    _patch_qdrant(monkeypatch)
    library = _library()
    movie = _movie("ARCH-006")
    source = _media(library, movie=movie, file_name="source.mp4")
    target = _media(library, movie=movie, file_name="target.mp4")
    _persist_thumbnails(source)
    _persist_thumbnails(target)
    generated_media_ids: list[int] = []

    class GeneratingStorage:
        def generate_thumbnails(self, *, media, workspace, progress_callback=None):
            generated_media_ids.append(media.media_id)
            source_path = workspace / "0.webp"
            source_path.write_bytes(_webp_bytes())
            return ThumbnailGeneration(
                expected_count=1,
                artifacts=(ThumbnailArtifact(offset_seconds=0, relative_path="0.webp"),),
            )

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: GeneratingStorage()
    )

    reset_count = MediaService.force_reset_media_thumbnails([target.id])

    assert reset_count == 1
    assert Media.get_by_id(target.id).thumbnail_adoption_blocked is True
    assert MediaThumbnail.select().where(MediaThumbnail.media == target).count() == 0

    outcome = MediaThumbnailTaskService._generate_one_locked(target.id)

    assert outcome.state == "succeeded"
    assert generated_media_ids == [target.id]
    assert MediaThumbnail.select().where(MediaThumbnail.media == target).count() == 1
    assert Media.get_by_id(target.id).thumbnail_adoption_blocked is False
