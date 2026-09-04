from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.model import Media, MediaLibrary, Movie, VideoItem
from src.plugins.provider_protocol import (
    ImportFile,
    ProviderOperationError,
    StagedMedia,
)
from src.service.transfers.imports.import_service import MediaImportService


def _source() -> ImportFile:
    return ImportFile(
        source_ref={"source": "origin"},
        name="video.mp4",
        relative_path="video.mp4",
        size_bytes=100,
        is_video=True,
    )


def _staged() -> StagedMedia:
    return StagedMedia(
        storage_ref={"target": "video.mp4"},
        receipt={"receipt": "video"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )


def _library() -> MediaLibrary:
    return MediaLibrary.create(name="identity-library", provider_key="test", provider_config={})


def test_import_skips_existing_provider_source_identity_before_staging(test_db):
    library = _library()
    video = VideoItem.create(title="existing")
    Media.create(
        library=library,
        video_item=video,
        file_name="existing.mp4",
        import_source_identity="provider-origin-v1",
    )

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (_source(),)

        def get_import_source_identity(self, *, source):
            assert source == _source()
            return "provider-origin-v1"

        def stage_import_file(self, **_kwargs):
            raise AssertionError("known source must not be staged")

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "directory"}, library.id, media_kind="video")

    assert result.imported_count == 0
    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert Media.select().count() == 1


def test_import_keeps_existing_behavior_when_provider_lacks_source_identity(test_db):
    library = _library()
    video = VideoItem.create(title="existing")
    Media.create(
        library=library,
        video_item=video,
        file_name="existing.mp4",
        import_source_identity="provider-origin-v1",
    )
    staged_calls = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (_source(),)

        def stage_import_file(self, **kwargs):
            staged_calls.append(kwargs["source"])
            return _staged()

        def finalize_import(self, *, receipt):
            assert receipt == _staged().receipt

        def compute_file_hash(self, *, media):
            return "f" * 64

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "directory"}, library.id, media_kind="video")

    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert staged_calls == [_source()]
    assert Media.select().count() == 2


def test_import_falls_back_to_per_file_handling_when_source_identity_fails(test_db):
    library = _library()
    missing_source = _source()
    available_source = ImportFile(
        source_ref={"source": "available"},
        name="available.mp4",
        relative_path="available.mp4",
        size_bytes=100,
        is_video=True,
    )
    staged_sources = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (missing_source, available_source)

        def get_import_source_identity(self, *, source):
            if source == missing_source:
                raise ProviderOperationError(
                    provider_key="test",
                    operation="get_import_source_identity",
                    code="source_not_found",
                    safe_message="source missing",
                )
            return "provider-available-v1"

        def stage_import_file(self, **kwargs):
            source = kwargs["source"]
            staged_sources.append(source)
            if source == missing_source:
                raise ProviderOperationError(
                    provider_key="test",
                    operation="stage_import_file",
                    code="source_not_found",
                    safe_message="source missing",
                )
            return _staged()

        def finalize_import(self, *, receipt):
            assert receipt == _staged().receipt

        def compute_file_hash(self, *, media):
            return "f" * 64

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "directory"}, library.id, media_kind="video")

    assert staged_sources == [missing_source, available_source]
    assert result.imported_count == 1
    assert result.skipped_count == 0
    assert result.failed_count == 1
    assert Media.select().count() == 1


def test_import_persists_source_identity_only_after_provider_finalizes(test_db):
    library = _library()
    finalized_with_identity = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (_source(),)

        def get_import_source_identity(self, *, source):
            return "provider-origin-v1"

        def stage_import_file(self, **_kwargs):
            return _staged()

        def finalize_import(self, *, receipt):
            assert receipt == _staged().receipt
            finalized_with_identity.append(Media.select().get().import_source_identity)

        def compute_file_hash(self, *, media):
            return "f" * 64

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source({"source": "directory"}, library.id, media_kind="video")

    assert result.imported_count == 1
    assert finalized_with_identity == [None]
    assert Media.select().get().import_source_identity == "provider-origin-v1"


@pytest.mark.parametrize("media_kind", ["jav", "video"])
@pytest.mark.parametrize("failure_at", [None, "write", "finalize"])
def test_import_preserves_plugin_finalize_and_abort_contract(test_db, monkeypatch, media_kind, failure_at):
    library = _library()
    movie = Movie.create(movie_number="TEST-001", title="test")
    source = ImportFile(
        source_ref={"source": "origin"}, name="TEST-001.mp4",
        relative_path="TEST-001.mp4", size_bytes=100, is_video=True,
    )
    staged = _staged()
    events = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (source,)

        def stage_import_file(self, *, source, placement, source_disposition, operation_key):
            assert source_disposition == "keep"
            events.append("stage")
            return staged

        def compute_file_hash(self, *, media):
            events.append("write")
            if failure_at == "write":
                raise RuntimeError("write_failed")
            return "f" * 64

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt
            assert Media.select().count() == 1
            events.append("finalize")
            if failure_at == "finalize":
                raise RuntimeError("finalize_failed")

        def abort_import(self, *, receipt):
            assert receipt == staged.receipt
            assert Media.select().count() == 0
            assert VideoItem.select().count() == 0
            events.append("abort")

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 0)
    monkeypatch.setattr(
        MediaImportService, "metadata_import_batch",
        lambda _self, _numbers: nullcontext({
            "TEST-001": SimpleNamespace(result=lambda: SimpleNamespace(movie_id=movie.id)),
        }),
    )
    service = MediaImportService(provider=Storage(), catalog_import_service=object())
    if failure_at == "finalize":
        with pytest.raises(RuntimeError, match="finalize_failed"):
            service.import_from_source({"source": "directory"}, library.id, media_kind=media_kind)
        assert events == ["stage", "write", "finalize"]
        assert Media.select().count() == 1
    else:
        result = service.import_from_source({"source": "directory"}, library.id, media_kind=media_kind)
        if failure_at == "write":
            assert result.failed_count == 1
            assert result.imported_count == 0
            assert events == ["stage", "write", "abort"]
        else:
            assert result.failed_count == 0
            assert result.imported_count == 1
            assert events == ["stage", "write", "finalize"]
