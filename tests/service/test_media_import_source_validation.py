import pytest

from src.api.exception.errors import ApiError
from src.model import MediaLibrary
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
    return MediaLibrary.create(
        name="source-validation-library", provider_key="test", provider_config={}
    )


def _overlap_error() -> ProviderOperationError:
    return ProviderOperationError(
        provider_key="local",
        operation="validate_import_source",
        code="invalid_config",
        safe_message="导入源与媒体库目录重叠",
        retryable=False,
    )


def test_import_from_source_validates_before_scanning(test_db):
    library = _library()
    calls = []

    class Storage:
        def validate_import_source(self, *, source_ref, source_disposition):
            calls.append(("validate", source_ref, source_disposition))
            raise _overlap_error()

        def scan_import_source(self, *, source_ref, progress_callback=None):
            calls.append(("scan", source_ref))
            return (_source(),)

    with pytest.raises(ApiError) as caught:
        MediaImportService(
            provider=Storage(), catalog_import_service=object()
        ).import_from_source(
            {"kind": "overlap"},
            library.id,
            media_kind="video",
        )

    assert caught.value.status_code == 422
    assert caught.value.code == "provider_invalid_config"
    assert calls == [("validate", {"kind": "overlap"}, "keep")]


def test_import_from_source_without_validation_capability_still_imports(test_db):
    library = _library()
    staged_calls = []

    class Storage:
        def scan_import_source(self, *, source_ref, progress_callback=None):
            return (_source(),)

        def stage_import_file(
            self, *, source, placement, source_disposition, operation_key
        ):
            staged_calls.append(source)
            return _staged()

        def finalize_import(self, *, receipt):
            assert receipt == _staged().receipt

        def compute_file_hash(self, *, media):
            return "f" * 64

    result = MediaImportService(
        provider=Storage(), catalog_import_service=object()
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert staged_calls == [_source()]


def test_retry_failed_file_validates_before_metadata_and_staging(test_db):
    library = _library()
    calls = []

    class Storage:
        def validate_import_source(self, *, source_ref, source_disposition):
            calls.append(("validate", source_ref, source_disposition))
            raise _overlap_error()

        def stage_import_file(self, **_kwargs):
            calls.append(("stage",))
            return _staged()

    failure_item = {
        "source_ref": {"kind": "overlap"},
        "name": "ABC-001.mp4",
        "relative_path": "release/ABC-001.mp4",
        "size_bytes": 100,
        "is_video": True,
        "media_kind": "jav",
        "library_id": library.id,
        "source_disposition": "keep",
    }

    with pytest.raises(ApiError) as caught:
        MediaImportService(
            provider=Storage(), catalog_import_service=object()
        ).retry_failed_file(
            failure_item,
            "plugin:demo:ABC-001",
            operation_key="retry-operation",
        )

    assert caught.value.status_code == 422
    assert caught.value.code == "provider_invalid_config"
    assert calls == [("validate", {"kind": "overlap"}, "keep")]
