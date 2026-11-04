import pytest

from src.common.media_import_status import (
    FAILURE_REASON_MEDIA_PLAYBACK_CONTAINER_INVALID,
    FAILURE_REASON_MEDIA_PLAYBACK_FILE_MISSING,
    FAILURE_REASON_MEDIA_PLAYBACK_INTERLEAVE_BAD,
    FAILURE_REASON_MEDIA_PLAYBACK_INVALID,
    FAILURE_REASON_MEDIA_PLAYBACK_MOOV_MISSING,
    FAILURE_REASON_MEDIA_PLAYBACK_VIDEO_STREAM_MISSING,
)
from src.model import Media, MediaLibrary, VideoItem
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.playback.media_playback_health_service import (
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
    ("health", "expected_reason"),
    [
        (
            PlaybackHealth(status=STATUS_INVALID, reason=REASON_FILE_MISSING),
            FAILURE_REASON_MEDIA_PLAYBACK_FILE_MISSING,
        ),
        (
            PlaybackHealth(status=STATUS_INVALID, reason=REASON_CONTAINER_INVALID),
            FAILURE_REASON_MEDIA_PLAYBACK_CONTAINER_INVALID,
        ),
        (
            PlaybackHealth(status=STATUS_INVALID, reason=REASON_MOOV_MISSING),
            FAILURE_REASON_MEDIA_PLAYBACK_MOOV_MISSING,
        ),
        (
            PlaybackHealth(
                status=STATUS_INVALID,
                reason=REASON_VIDEO_STREAM_MISSING,
            ),
            FAILURE_REASON_MEDIA_PLAYBACK_VIDEO_STREAM_MISSING,
        ),
        (
            PlaybackHealth(
                status=STATUS_INVALID,
                reason=REASON_INTERLEAVE_BAD,
                detail={"max_gap_mib": 1024.0, "threshold_mib": 64.0},
            ),
            FAILURE_REASON_MEDIA_PLAYBACK_INTERLEAVE_BAD,
        ),
        (
            PlaybackHealth(status=STATUS_INVALID),
            FAILURE_REASON_MEDIA_PLAYBACK_INVALID,
        ),
    ],
)
def test_import_rejects_definitive_playback_invalid(
    test_db,
    monkeypatch,
    health,
    expected_reason,
):
    library = MediaLibrary.create(
        name="import-health",
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

    assert result.imported_count == 0
    assert result.failed_count == 1
    assert result.new_playable_movies == []
    assert result.failed_files[0]["reason"] == expected_reason
    assert result.failed_files[0]["kind"] == "file"
    assert Media.select().count() == 0
    assert VideoItem.select().count() == 0
    assert events == ["abort"]


def test_import_rejection_detail_contains_interleave_metrics(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="import-health-detail",
        provider_key="legacy-demo",
        provider_config={},
    )
    events: list[str] = []
    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "probe_media",
        staticmethod(
            lambda media, storage=None: PlaybackHealth(
                status=STATUS_INVALID,
                reason=REASON_INTERLEAVE_BAD,
                detail={"max_gap_mib": 1024.0, "threshold_mib": 64.0},
            )
        ),
    )

    result = MediaImportService(
        provider=_storage(events),
        catalog_import_service=object(),
    ).import_from_source(
        {"source": "directory"},
        library.id,
        media_kind="video",
    )

    detail = result.failed_files[0]["detail"]
    assert "reason=interleave_bad" in detail
    assert "max_gap_mib=1024.0" in detail
    assert "threshold_mib=64.0" in detail


@pytest.mark.parametrize(
    "health",
    [
        PlaybackHealth(status=STATUS_OK),
        PlaybackHealth(
            status=STATUS_UNKNOWN,
            detail={"error_type": "provider_unavailable"},
        ),
    ],
)
def test_import_accepts_ok_and_unknown_health(
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
    assert events == ["finalize"]
