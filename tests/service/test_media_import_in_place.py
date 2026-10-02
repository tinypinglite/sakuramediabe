import pytest

from src.api.exception.errors import ApiError
from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ImportFile,
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
        storage_ref={"kind": "in_place_media"},
        receipt={"receipt": "in-place"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )


def _library() -> MediaLibrary:
    return MediaLibrary.create(
        name="in-place-library", provider_key="test", provider_config={}
    )


def test_in_place_import_passes_disposition_and_records_identity(test_db):
    library = _library()
    seen_dispositions = []

    class Storage:
        supports_in_place_import = True

        def scan_import_source(self, *, source_ref):
            return (_source(),)

        def get_import_source_identity(self, *, source):
            return "in-place-origin-v1"

        def stage_import_file(
            self, *, source, placement, source_disposition, operation_key
        ):
            seen_dispositions.append(source_disposition)
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
        source_disposition="in_place",
    )

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert seen_dispositions == ["in_place"]
    assert Media.select().get().import_source_identity == "in-place-origin-v1"


def test_in_place_import_rejected_when_provider_lacks_capability(test_db):
    library = _library()
    calls = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            calls.append("scan")
            return (_source(),)

    with pytest.raises(ApiError) as exc:
        MediaImportService(
            provider=Storage(), catalog_import_service=object()
        ).import_from_source(
            {"source": "directory"},
            library.id,
            media_kind="video",
            source_disposition="in_place",
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "in_place_import_unsupported"
    assert calls == []


def test_in_place_import_uses_bundle_capability_without_override(test_db, monkeypatch):
    library = _library()
    seen_dispositions = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (_source(),)

        def get_import_source_identity(self, *, source):
            return "bundle-origin-v1"

        def stage_import_file(
            self, *, source, placement, source_disposition, operation_key
        ):
            seen_dispositions.append(source_disposition)
            return _staged()

        def finalize_import(self, *, receipt):
            pass

        def compute_file_hash(self, *, media):
            return "f" * 64

    monkeypatch.setattr(
        MediaImportService, "_storage", lambda self, _library: Storage()
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "supports_in_place_import", lambda _key: True
    )

    result = MediaImportService(catalog_import_service=object()).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
        source_disposition="in_place",
    )

    assert result.imported_count == 1
    assert seen_dispositions == ["in_place"]
