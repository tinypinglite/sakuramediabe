from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.model import MediaLibrary
from src.plugins.provider_protocol import ImportFile
from src.service.transfers.imports.import_service import MediaImportService


class _StopImport(Exception):
    pass


def test_jav_import_only_resolves_target_movie_number(monkeypatch):
    requested: list[str] = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return tuple(
                ImportFile(
                    source_ref={"id": name},
                    name=name,
                    relative_path=name,
                    size_bytes=100,
                    is_video=True,
                )
                for name in ("TEST-001.mp4", "TEST-002.mp4")
            )

    def metadata_batch(_self, numbers):
        requested.extend(numbers)
        raise _StopImport

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 0)
    monkeypatch.setattr(MediaImportService, "metadata_import_batch", metadata_batch)
    monkeypatch.setattr(
        MediaLibrary, "get_or_none", lambda *_args, **_kwargs: SimpleNamespace(id=1)
    )
    service = MediaImportService(provider=Storage(), catalog_import_service=object())

    with pytest.raises(_StopImport):
        service.import_from_source(
            {"source": "directory"},
            1,
            media_kind="jav",
            target_movie_number="TEST-001",
        )

    assert requested == ["TEST-001"]


def test_jav_target_import_uses_target_when_filename_unparseable(monkeypatch):
    requested: list[str] = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (
                ImportFile(
                    source_ref={"id": "no-number"},
                    name="sample video no number.mp4",
                    relative_path="sample video no number.mp4",
                    size_bytes=100,
                    is_video=True,
                ),
            )

    def metadata_batch(_self, numbers):
        requested.extend(numbers)
        raise _StopImport

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 0)
    monkeypatch.setattr(MediaImportService, "metadata_import_batch", metadata_batch)
    monkeypatch.setattr(
        MediaLibrary, "get_or_none", lambda *_args, **_kwargs: SimpleNamespace(id=1)
    )
    service = MediaImportService(provider=Storage(), catalog_import_service=object())

    with pytest.raises(_StopImport):
        service.import_from_source(
            {"source": "directory"},
            1,
            media_kind="jav",
            target_movie_number="TEST-001",
        )

    assert requested == ["TEST-001"]


def test_jav_target_import_records_mismatched_video(monkeypatch):
    events: list[dict] = []

    class Storage:
        def scan_import_source(self, *, source_ref):
            return (
                ImportFile(
                    source_ref={"id": "noise"},
                    name="QR-1024.jpg",
                    relative_path="QR-1024.jpg",
                    size_bytes=100,
                    is_video=False,
                ),
                ImportFile(
                    source_ref={"id": "other"},
                    name="TEST-002.mp4",
                    relative_path="TEST-002.mp4",
                    size_bytes=100,
                    is_video=True,
                ),
            )

    def metadata_batch(_self, numbers):
        raise _StopImport

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 0)
    monkeypatch.setattr(MediaImportService, "metadata_import_batch", metadata_batch)
    monkeypatch.setattr(
        MediaLibrary,
        "get_or_none",
        lambda *_args, **_kwargs: SimpleNamespace(id=1, provider_key=""),
    )
    service = MediaImportService(provider=Storage(), catalog_import_service=object())

    with pytest.raises(_StopImport):
        service.import_from_source(
            {"source": "directory"},
            1,
            media_kind="jav",
            target_movie_number="TEST-001",
            progress_callback=events.append,
        )

    scan_event = next(event for event in events if event.get("event") == "scan_complete")
    items = scan_event["summary_patch"]["failed_files"]
    assert [(item["relative_path"], item["reason"], item["kind"]) for item in items] == [
        ("TEST-002.mp4", "target_movie_number_mismatch", "skipped")
    ]
    assert scan_event["summary_patch"]["skipped_count"] == 1
