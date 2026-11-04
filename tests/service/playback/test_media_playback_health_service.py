from __future__ import annotations

import pytest

from src.config.config import settings
from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import ProviderOperationError
from src.service.playback.media_playback_health_service import (
    MAX_READ_BYTES,
    REASON_CONTAINER_INVALID,
    REASON_FILE_MISSING,
    REASON_INTERLEAVE_BAD,
    REASON_MOOV_MISSING,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_UNKNOWN,
    MediaPlaybackHealthService,
    PlaybackHealth,
    _PyAvReaderAdapter,
)


class BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._position
        result = self._data[self._position : self._position + size]
        self._position += len(result)
        return result

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1:
            offset += self._position
        elif whence == 2:
            offset += len(self._data)
        self._position = max(0, offset)
        return self._position

    def close(self) -> None:
        return None


def _box(name: str, payload: bytes = b"") -> bytes:
    size = 8 + len(payload)
    return size.to_bytes(4, "big") + name.encode("latin1") + payload


def _media(*, valid: bool = True) -> Media:
    library = MediaLibrary.create(
        name=f"health-{Media.select().count()}",
        provider_key="demo",
    )
    movie = Movie.create(
        movie_number=f"HEALTH-{Media.select().count()}",
        javdb_id=f"health-{Media.select().count()}",
        title="health",
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name="health.mp4",
        file_size_bytes=100,
        duration_seconds=60,
        valid=valid,
    )


def test_has_moov_reads_supported_sizes_and_ignores_trailing_padding():
    payload = _box("ftyp") + _box("moov", b"x") + _box("mdat", b"1234") + b"\0"

    found = MediaPlaybackHealthService.has_moov(
        BytesReader(payload),
        len(payload),
    )

    assert found is True


def test_has_moov_stops_before_large_fragmented_mp4_box_series():
    payload = _box("ftyp") + _box("moov")
    for _ in range(300):
        payload += _box("moof") + _box("mdat", b"x")

    assert MediaPlaybackHealthService.has_moov(
        BytesReader(payload),
        len(payload),
    ) is True


def test_pyav_adapter_rewinds_the_underlying_reader():
    reader = BytesReader(b"abc")
    reader.seek(3)

    adapter = _PyAvReaderAdapter(reader, file_size=3)

    assert adapter.read(1) == b"a"


def test_probe_reader_rejects_missing_moov_and_accepts_trailing_moov(monkeypatch):
    missing_moov = _box("ftyp") + _box("mdat")
    missing = MediaPlaybackHealthService.probe_reader(
        BytesReader(missing_moov),
        file_name="missing.mp4",
        file_size=len(missing_moov),
        duration_seconds=60,
    )
    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "sample_interleave",
        classmethod(
            lambda cls, reader, *, file_size, duration_seconds: PlaybackHealth(
                status=STATUS_OK
            )
        ),
    )
    trailing_moov = _box("ftyp") + _box("mdat") + _box("moov")
    trailing = MediaPlaybackHealthService.probe_reader(
        BytesReader(trailing_moov),
        file_name="trailing.mp4",
        file_size=len(trailing_moov),
        duration_seconds=60,
    )

    assert missing.status == STATUS_INVALID
    assert missing.reason == REASON_MOOV_MISSING
    assert trailing.status == STATUS_OK


def test_probe_reader_marks_invalid_box_size_as_container_invalid():
    payload = (4).to_bytes(4, "big") + b"mdat"

    health = MediaPlaybackHealthService.probe_reader(
        BytesReader(payload),
        file_name="broken.mp4",
        file_size=len(payload),
        duration_seconds=60,
    )

    assert health.status == STATUS_INVALID
    assert health.reason == REASON_CONTAINER_INVALID


def test_non_iso_container_skips_box_checks(monkeypatch):
    calls = []

    def fake_sample(cls, reader, *, file_size, duration_seconds):
        calls.append((file_size, duration_seconds))
        return PlaybackHealth(status=STATUS_OK, detail={"sample_count": 10})

    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "sample_interleave",
        classmethod(fake_sample),
    )

    health = MediaPlaybackHealthService.probe_reader(
        BytesReader(b"not-an-mp4"),
        file_name="video.mkv",
        file_size=11,
        duration_seconds=60,
    )

    assert health.status == STATUS_OK
    assert calls == [(11, 60)]


def test_interleave_threshold_uses_configured_gap_mib(monkeypatch):
    monkeypatch.setattr(settings.media, "playback_interleave_max_gap_mib", 64.0)
    threshold_bytes = 64 * 1024 * 1024
    below = MediaPlaybackHealthService.health_from_samples(
        [{"percent": 50, "gap_bytes": threshold_bytes - 1}],
        video_stream_count=1,
        audio_stream_count=1,
    )
    at_threshold = MediaPlaybackHealthService.health_from_samples(
        [{"percent": 50, "gap_bytes": threshold_bytes}],
        video_stream_count=2,
        audio_stream_count=1,
    )

    assert below.status == STATUS_OK
    assert below.detail["max_gap_mib"] == 64.0
    assert below.detail["threshold_mib"] == 64.0
    assert at_threshold.status == STATUS_INVALID
    assert at_threshold.reason == REASON_INTERLEAVE_BAD
    assert at_threshold.detail["video_stream_count"] == 2
    assert at_threshold.detail["max_gap_percent"] == 50


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), "64"])
def test_interleave_gap_ignores_missing_or_invalid_detail(value):
    health = (
        None
        if value is None
        else {"detail": {"max_gap_mib": value}}
    )

    assert MediaPlaybackHealthService.interleave_gap_mib(health) is None


def test_interleave_gap_reads_numeric_detail():
    health = {"detail": {"max_gap_mib": 64.5}}

    assert MediaPlaybackHealthService.interleave_gap_mib(health) == 64.5


def test_pyav_adapter_caps_single_read_size():
    class RecordingReader(BytesReader):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.requested_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            return super().read(size)

    reader = RecordingReader(b"x" * (MAX_READ_BYTES + 1))
    adapter = _PyAvReaderAdapter(reader, file_size=len(reader._data))

    assert len(adapter.read(MAX_READ_BYTES + 1)) == MAX_READ_BYTES
    assert reader.requested_sizes == [MAX_READ_BYTES]


def test_provider_budget_error_is_unknown(test_db):
    media = _media()

    class Storage:
        @staticmethod
        def open_cover_source(*, media):
            raise RuntimeError("读取超过预算")

    health = MediaPlaybackHealthService.probe_media(media, storage=Storage())

    assert health.status == STATUS_UNKNOWN
    assert health.detail["error_type"] == "provider_budget_exceeded"


@pytest.mark.parametrize(
    ("provider_code", "expected_status", "expected_reason"),
    [
        ("source_not_found", STATUS_INVALID, REASON_FILE_MISSING),
        ("unavailable", STATUS_UNKNOWN, None),
        ("authentication_failed", STATUS_UNKNOWN, None),
        ("unsupported", STATUS_UNKNOWN, None),
    ],
)
def test_provider_probe_errors_only_missing_source_is_definitive(
    test_db,
    provider_code,
    expected_status,
    expected_reason,
):
    media = _media()

    class Storage:
        @staticmethod
        def open_cover_source(*, media):
            raise ProviderOperationError(
                provider_key="demo",
                operation="open_cover_source",
                code=provider_code,
                safe_message="provider error",
                retryable=True,
            )

    health = MediaPlaybackHealthService.probe_media(media, storage=Storage())

    assert health.status == expected_status
    assert health.reason == expected_reason


def test_probe_media_uses_cover_source_without_transfer_source(
    test_db,
    monkeypatch,
):
    media = _media()

    class Storage:
        @staticmethod
        def open_cover_source(*, media):
            return BytesReader(_box("ftyp") + _box("moov", b"x" * 84))

        @staticmethod
        def open_transfer_source(*, media):
            raise AssertionError("health probe must not use transfer source")

    monkeypatch.setattr(
        MediaPlaybackHealthService,
        "sample_interleave",
        classmethod(
            lambda cls, reader, *, file_size, duration_seconds: PlaybackHealth(
                status=STATUS_OK
            )
        ),
    )

    health = MediaPlaybackHealthService.probe_media(media, storage=Storage())

    assert health.status == STATUS_OK


@pytest.mark.parametrize("valid", [True, False])
def test_write_health_does_not_change_media_validity(test_db, valid):
    media = _media(valid=valid)
    health = PlaybackHealth(
        status=STATUS_INVALID,
        reason=REASON_INTERLEAVE_BAD,
        detail={"max_gap_mib": 1024},
    )

    MediaPlaybackHealthService.write_health(media, health)

    stored = Media.get_by_id(media.id)
    assert stored.valid is valid
    assert stored.playback_health["reason"] == REASON_INTERLEAVE_BAD
