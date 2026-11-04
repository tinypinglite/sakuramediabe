import pytest

from src.model import Media, MediaLibrary
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.playback.media_playback_health_service import (
    MIN_PLAYBACK_PROBE_FILE_SIZE_BYTES,
    REASON_CONTAINER_INVALID,
    REASON_FILE_MISSING,
    REASON_INTERLEAVE_BAD,
    REASON_MOOV_MISSING,
    REASON_VIDEO_STREAM_MISSING,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_UNKNOWN,
    MediaPlaybackHealthService,
    PlaybackHealth,
)
from src.service.transfers.imports.import_service import MediaImportService


def _source() -> ImportFile:
    return ImportFile(
        source_ref={"source": "file"},
        name="video.mp4",
        relative_path="video.mp4",
        size_bytes=100,
        is_video=True,
    )


def _staged() -> StagedMedia:
    return StagedMedia(
        storage_ref={"kind": "staged"},
        receipt={"receipt": "video"},
        size_bytes=100,
        duration_seconds=60,
        video_info=None,
    )


def _storage(events: list[str]):
    source = _source()
    staged = _staged()

    class Storage:
        def scan_import_source(self, *, source_ref):
            assert source_ref == {"source": "directory"}
            return (source,)

        def stage_import_file(self, **_kwargs):
            return staged

        def compute_file_hash(self, *, media):
            return "media-file-hash-v1:" + "a" * 40

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt
            events.append("finalize")

        def abort_import(self, *, receipt):
            assert receipt == staged.receipt
            events.append("abort")

    return Storage()


@pytest.mark.parametrize(
    "health",
    [
        PlaybackHealth(status=STATUS_OK),
        PlaybackHealth(
            status=STATUS_UNKNOWN,
            detail={"error_type": "provider_unavailable"},
        ),
        PlaybackHealth(status=STATUS_INVALID, reason=REASON_FILE_MISSING),
        PlaybackHealth(status=STATUS_INVALID, reason=REASON_CONTAINER_INVALID),
        PlaybackHealth(status=STATUS_INVALID, reason=REASON_MOOV_MISSING),
        PlaybackHealth(
            status=STATUS_INVALID,
            reason=REASON_VIDEO_STREAM_MISSING,
        ),
        PlaybackHealth(
            status=STATUS_INVALID,
            reason=REASON_INTERLEAVE_BAD,
            detail={"max_gap_mib": 1024.0, "threshold_mib": 64.0},
        ),
        PlaybackHealth(status=STATUS_INVALID),
    ],
)
def test_import_records_every_playback_health_status(
    test_db,
    monkeypatch,
    health,
):
    library = MediaLibrary.create(
        name="import-health-accepted",
        provider_key="legacy-demo",
        provider_config={},
    )
    events: list[str] = []
    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "probe_media",
        staticmethod(lambda media, storage=None: health),
    )

    result = MediaImportService(
        provider=_storage(events),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    assert result.imported_count == 1
    assert result.failed_count == 0
    media = Media.get()
    assert media.valid is True
    assert media.playback_health["status"] == health.status
    assert media.playback_health["reason"] == health.reason
    assert media.playback_health["detail"] == health.detail
    assert events == ["finalize"]


def test_import_continues_when_playback_probe_raises(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="import-health-probe-failure",
        provider_key="legacy-demo",
        provider_config={},
    )
    events: list[str] = []

    def fail_probe(media, storage=None):
        raise RuntimeError("probe unavailable")

    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "probe_media",
        staticmethod(fail_probe),
    )

    result = MediaImportService(
        provider=_storage(events),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    assert result.imported_count == 1
    assert result.failed_count == 0
    assert Media.get().playback_health is None
    assert events == ["finalize"]


def test_import_small_file_records_ok_without_provider_probe(test_db):
    library = MediaLibrary.create(
        name="import-health-small-file",
        provider_key="legacy-demo",
        provider_config={},
    )
    events: list[str] = []

    result = MediaImportService(
        provider=_storage(events),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    media = Media.get()
    assert result.imported_count == 1
    assert result.failed_count == 0
    assert media.file_size_bytes < MIN_PLAYBACK_PROBE_FILE_SIZE_BYTES
    assert media.playback_health["status"] == "ok"
    assert media.playback_health["reason"] is None
    assert media.playback_health["detail"] == {
        "skip_reason": "file_too_small",
        "file_size_bytes": 100,
        "probe_min_file_size_bytes": MIN_PLAYBACK_PROBE_FILE_SIZE_BYTES,
    }
    assert media.playback_health["checked_at"]
    assert events == ["finalize"]
