from contextlib import nullcontext
from types import SimpleNamespace

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.transfers.imports.import_service import MediaImportService


def test_retry_uses_persisted_file_ref_and_associates_media(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="retry-storage-library", provider_key="test", provider_config={}
    )
    movie = Movie.create(movie_number="ABC-001", title="selected movie")
    staged = StagedMedia(
        storage_ref={"stored": "media"},
        receipt={"receipt": "retry"},
        size_bytes=100,
        duration_seconds=90,
        video_info=None,
    )
    events = []
    stored_source = []

    class Storage:
        def stage_import_file(self, *, source, placement, source_disposition, operation_key):
            stored_source.append((source, placement.relative_path, operation_key))
            events.append("stage")
            return staged

        def compute_file_hash(self, *, media):
            events.append("write")
            return "f" * 64

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt
            events.append("finalize")

    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service.MovieMetadataSearchService.fetch_candidate",
        lambda _candidate_id: nullcontext(
            (SimpleNamespace(movie_number="ABC-001"), "javdb", object(), None)
        ),
    )
    catalog = SimpleNamespace(
        import_movie_if_missing=lambda _detail, force_subscribed: (movie, False),
    )
    failure_item = {
        "source_ref": {"file": "opaque-file"},
        "name": "ABC-001.mp4",
        "relative_path": "release/ABC-001.mp4",
        "size_bytes": 100,
        "is_video": True,
        "media_kind": "jav",
        "library_id": library.id,
        "source_disposition": "keep",
    }

    result = MediaImportService(
        provider=Storage(), catalog_import_service=catalog
    ).retry_failed_file(
        failure_item,
        "javdb:ABC-001:javdb-001",
        operation_key="retry-operation",
    )

    assert result["movie_id"] == movie.id
    assert result["media_id"] == Media.select().get().id
    assert events == ["stage", "write", "finalize"]
    source, placement, operation_key = stored_source[0]
    assert source == ImportFile(
        source_ref={"file": "opaque-file"},
        name="ABC-001.mp4",
        relative_path="release/ABC-001.mp4",
        size_bytes=100,
        is_video=True,
    )
    assert placement == "jav/ABC-001/ABC-001.mp4"
    assert operation_key == "retry-operation"
