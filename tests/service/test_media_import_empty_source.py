from contextlib import nullcontext
from types import SimpleNamespace

from src.config.config import settings
from src.model import MediaLibrary
from src.plugins.provider_protocol import ImportFile, ImportFileContent, StagedMedia
from src.schema.catalog.subtitles import SubtitleImportResult, SubtitleImportStatus
from src.service.transfers.imports import import_service
from src.service.transfers.imports.import_service import MediaImportService


def test_provider_import_with_no_scanned_files_is_noop(test_db):
    class EmptyStorageProvider:
        def scan_import_source(self, *, source_ref):
            assert source_ref == {"source": "empty"}
            return ()

    library = MediaLibrary.create(
        name="Library",
        provider_key="test",
        provider_config={},
    )
    service = MediaImportService(
        provider=EmptyStorageProvider(),
        catalog_import_service=object(),
    )
    result = service.import_from_source(
        {"source": "empty"},
        library.id,
    )

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.failed_count == 0


def test_provider_import_skips_small_and_non_video_files(monkeypatch):
    class FilteringStorageProvider:
        def scan_import_source(self, *, source_ref):
            return (
                ImportFile(
                    source_ref={"id": "small"},
                    name="ABP-001.mp4",
                    relative_path="ABP-001.mp4",
                    size_bytes=99,
                    is_video=True,
                ),
                ImportFile(
                    source_ref={"id": "text"},
                    name="readme.txt",
                    relative_path="readme.txt",
                    size_bytes=4096,
                    is_video=False,
                ),
            )

        def stage_import_file(self, **_kwargs):
            raise AssertionError("filtered files must not be staged")

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 100)
    library = SimpleNamespace(id=1)
    monkeypatch.setattr(MediaLibrary, "get_or_none", lambda *_args, **_kwargs: library)
    service = MediaImportService(
        provider=FilteringStorageProvider(),
        catalog_import_service=object(),
    )

    result = service.import_from_source({"source": "filtered"}, library.id)

    assert result.imported_count == 0
    assert result.skipped_count == 2
    assert result.failed_count == 0


def test_video_import_generates_cover_after_finalizing(monkeypatch):
    events: list[str] = []
    source = ImportFile(
        source_ref={"id": "video"},
        name="video.mp4",
        relative_path="video.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"id": "stored-video"},
        receipt={"id": "receipt"},
        size_bytes=100,
        duration_seconds=None,
        video_info=None,
    )
    library = SimpleNamespace(
        id=1,
        provider_key="test",
        provider_config={},
        account_key=None,
    )
    video = SimpleNamespace(id=7)
    media = object()
    cover_source = object()

    class Storage:
        def scan_import_source(self, *, source_ref):
            assert source_ref == {"source": "video"}
            return (source,)

        def stage_import_file(self, **_kwargs):
            return staged

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt
            events.append("finalize")

        def open_cover_source(self, *, media):
            assert media == "media-handle"
            events.append("open_cover_source")
            return nullcontext(cover_source)

    def create_media(**kwargs):
        assert kwargs["video_item"] is video
        events.append("create_media")
        return media

    def generate_cover(actual_video, actual_source):
        assert actual_video is video
        assert actual_source is cover_source
        events.append("cover")

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(MediaLibrary, "get_or_none", lambda *_args, **_kwargs: library)
    monkeypatch.setattr(
        import_service,
        "get_database",
        lambda: SimpleNamespace(atomic=nullcontext),
    )
    monkeypatch.setattr(import_service.VideoItem, "create", lambda **_kwargs: video)
    monkeypatch.setattr(MediaImportService, "_create_media", staticmethod(create_media))
    monkeypatch.setattr(import_service, "media_handle_for", lambda _value: "media-handle")
    monkeypatch.setattr(import_service.VideoCoverService, "generate_cover", generate_cover)

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "video"}, library.id, media_kind="video")

    assert result.imported_count == 1
    assert result.created_video_ids == [video.id]
    assert events == ["create_media", "finalize", "open_cover_source", "cover"]


def test_import_sidecar_subtitles_matches_same_directory_and_movie_number(monkeypatch):
    video = ImportFile(
        source_ref={"id": "video"},
        name="ABC-001.mp4",
        relative_path="release/ABC-001.mp4",
        size_bytes=100,
        is_video=True,
    )
    matching = ImportFile(
        source_ref={"id": "matching"},
        name="ABC-001.chs.srt",
        relative_path="release/ABC-001.chs.srt",
        size_bytes=10,
        is_video=False,
    )
    other_directory = ImportFile(
        source_ref={"id": "other-directory"},
        name="ABC-001.srt",
        relative_path="other/ABC-001.srt",
        size_bytes=10,
        is_video=False,
    )
    other_movie = ImportFile(
        source_ref={"id": "other-movie"},
        name="ABC-002.srt",
        relative_path="release/ABC-002.srt",
        size_bytes=10,
        is_video=False,
    )
    reads = []
    deleted = []

    class Storage:
        def read_import_file(self, *, source):
            reads.append(source)
            return ImportFileContent(content=b"subtitle", deletion_receipt={"id": source.name})

        def delete_import_file(self, *, receipt):
            deleted.append(receipt)

    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.SubtitleAssetService.import_subtitle_content",
        lambda movie_number, content, filename: SubtitleImportResult(
            status=SubtitleImportStatus.IMPORTED,
            subtitle_id=1,
        ),
    )

    failures = MediaImportService._import_sidecar_subtitles(
        storage=Storage(),
        video_source=video,
        movie_number="ABC-001",
        subtitle_sources=(matching, other_directory, other_movie),
        imported_subtitle_paths=set(),
        source_disposition="delete_after_commit",
        failure_items=[],
    )

    assert failures == 0
    assert reads == [matching]
    assert deleted == [{"id": "ABC-001.chs.srt"}]
