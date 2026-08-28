from types import SimpleNamespace

from src.config.config import settings
from src.model import MediaLibrary
from src.plugins.provider_protocol import ImportFile, ImportFileContent
from src.schema.catalog.subtitles import SubtitleImportResult, SubtitleImportStatus
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
