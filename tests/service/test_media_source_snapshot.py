from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

from src.config.config import settings
from src.model import (
    DownloadClient,
    DownloadSubmissionRecord,
    DownloadTask,
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaTag,
    Movie,
)
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.transfers.imports.import_service import (
    MediaImportService,
    MetadataImportResult,
)

HASH = "0123456789abcdef0123456789abcdef01234567"


class Storage:
    def __init__(self, source: ImportFile, staged: StagedMedia):
        self.source = source
        self.staged = staged

    def scan_import_source(self, *, source_ref):
        return (self.source,)

    def stage_import_file(self, **_kwargs):
        return self.staged

    def finalize_import(self, *, receipt):
        assert receipt == self.staged.receipt

    def compute_file_hash(self, *, media):
        return "f" * 64


def _library(name: str) -> MediaLibrary:
    return MediaLibrary.create(name=name, provider_key="test", provider_config={})


def _download_source(library: MediaLibrary):
    client = DownloadClient.create(
        name=f"{library.name}-client",
        library=library,
        provider_config={},
    )
    task = DownloadTask.create(
        client=client,
        name="download task",
        movie="SNAP-001",
        remote_id="snapshot-task",
        state="completed",
        completed_source_ref={"source": "completed"},
    )
    DownloadSubmissionRecord.create(
        client_id=client.id,
        task_id=task.id,
        movie_number="SNAP-001",
        indexer_name="indexer-a",
        title="SNAP-001 1080p 中文字幕",
        release_title="SNAP-001 1080p",
        description="中文字幕 资源简介",
        source_uri=f"magnet:?xt=urn:btih:{HASH}",
        info_hash=HASH,
        state="submitted",
    )
    return task


def test_download_task_import_persists_snapshot_and_tags(test_db, monkeypatch):
    library = _library("snapshot-download")
    movie = Movie.create(movie_number="SNAP-001", title="snapshot")
    task = _download_source(library)
    source = ImportFile(
        source_ref={"id": "source"},
        name="SNAP-001.mp4",
        relative_path="SNAP-001.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"id": "staged"},
        receipt={"id": "receipt"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )
    MediaTag.create(
        name="中文字幕",
        include_terms=["中文字幕"],
        exclude_terms=[],
        enabled=True,
    )
    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 0)
    monkeypatch.setattr(
        MediaImportService,
        "_subscribe_imported_movie",
        staticmethod(lambda _movie: None),
    )

    @contextmanager
    def metadata_batch(_self, movie_numbers, *, thread_name_prefix="import-metadata"):
        yield {
            movie_number: SimpleNamespace(
                result=lambda number=movie_number: MetadataImportResult(
                    movie_number=number,
                    movie_id=movie.id,
                )
            )
            for movie_number in movie_numbers
        }

    monkeypatch.setattr(MediaImportService, "metadata_import_batch", metadata_batch)

    result = MediaImportService(
        provider=Storage(source, staged),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        download_task_id=task.id,
    )

    assert result.imported_count == 1
    media = Media.get()
    assert media.release_title == "SNAP-001 1080p"
    assert media.release_description == "中文字幕 资源简介"
    assert media.source_indexer == "indexer-a"
    assert media.info_hash == HASH
    assert media.download_task_id == task.id
    assert MediaMediaTag.get().tag.name == "中文字幕"


def test_manual_video_import_without_download_task_has_empty_snapshot(
    test_db,
    monkeypatch,
):
    library = _library("snapshot-video")
    source = ImportFile(
        source_ref={"id": "video"},
        name="video.mp4",
        relative_path="video.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"id": "staged-video"},
        receipt={"id": "video-receipt"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )
    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 101)
    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService._generate_video_cover",
        lambda *_args, **_kwargs: None,
    )

    result = MediaImportService(
        provider=Storage(source, staged),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    assert result.imported_count == 1
    media = Media.get()
    assert media.release_title is None
    assert media.release_description is None
    assert media.source_indexer is None
    assert media.info_hash is None
    assert media.download_task_id is None
    assert not MediaMediaTag.select().exists()


def test_failed_item_retry_restores_source_snapshot(test_db, monkeypatch):
    library = _library("snapshot-retry")
    movie = Movie.create(movie_number="SNAP-002", title="snapshot retry")
    task = _download_source(library)
    task.movie = "SNAP-002"
    task.save(only=[DownloadTask.movie])
    record = DownloadSubmissionRecord.get()
    record.movie_number = "SNAP-002"
    record.save(only=[DownloadSubmissionRecord.movie_number])
    source = ImportFile(
        source_ref={"file": "opaque"},
        name="SNAP-002.mp4",
        relative_path="SNAP-002.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged = StagedMedia(
        storage_ref={"stored": "retry"},
        receipt={"receipt": "retry"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )
    storage = Storage(source, staged)
    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service."
        "MovieMetadataSearchService.fetch_candidate",
        lambda _candidate_id: nullcontext(
            (SimpleNamespace(movie_number="SNAP-002"), "javdb", object(), None)
        ),
    )
    catalog = SimpleNamespace(
        import_movie_if_missing=lambda _detail, force_subscribed=False: (movie, False)
    )
    monkeypatch.setattr(
        MediaImportService,
        "_subscribe_imported_movie",
        staticmethod(lambda _movie: None),
    )
    failure_item = {
        "source_ref": {"file": "opaque"},
        "name": source.name,
        "relative_path": source.relative_path,
        "size_bytes": source.size_bytes,
        "is_video": True,
        "media_kind": "jav",
        "library_id": library.id,
        "source_disposition": "keep",
        "download_task_id": task.id,
    }

    result = MediaImportService(
        provider=storage,
        catalog_import_service=catalog,
    ).retry_failed_file(
        failure_item,
        "javdb:SNAP-002:source",
        operation_key="retry-source-snapshot",
    )

    media = Media.get_by_id(result["media_id"])
    assert media.release_title == "SNAP-001 1080p"
    assert media.release_description == "中文字幕 资源简介"
    assert media.source_indexer == "indexer-a"
    assert media.info_hash == HASH
    assert media.download_task_id == task.id
