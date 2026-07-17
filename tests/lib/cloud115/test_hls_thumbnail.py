from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image as PILImage

from src.lib.cloud115 import (
    Cloud115HlsSegmentReader,
    Cloud115RequestError,
    VideoDefinition,
    VideoSegment,
)
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.playback import media_thumbnail_service as thumbnail_module


def _segment(index: int, duration: float = 10.0) -> VideoSegment:
    return VideoSegment(
        index=index,
        url=f"https://cdn.example/{index}.ts",
        duration_seconds=duration,
    )


def test_hls_thumbnail_selects_lowest_resolution_then_bandwidth() -> None:
    definitions = [
        VideoDefinition(3_000_000, "1920x1080", "UD", "https://cdn/ud.m3u8"),
        VideoDefinition(900_000, "1280x720", "HD-high", "https://cdn/hd-high.m3u8"),
        VideoDefinition(600_000, "1280x720", "HD-low", "https://cdn/hd-low.m3u8"),
        VideoDefinition(300_000, "", "unknown", "https://cdn/unknown.m3u8"),
    ]

    selected = MediaThumbnailService._select_lowest_hls_definition(definitions)

    assert selected.label == "HD-low"


def test_hls_thumbnail_selects_lowest_bandwidth_when_resolutions_are_missing() -> None:
    definitions = [
        VideoDefinition(900_000, "", "high", "https://cdn/high.m3u8"),
        VideoDefinition(300_000, "invalid", "low", "https://cdn/low.m3u8"),
    ]

    selected = MediaThumbnailService._select_lowest_hls_definition(definitions)

    assert selected.label == "low"


def test_hls_timeline_keeps_fixed_offsets_and_reuses_same_segment() -> None:
    targets, expected_count = MediaThumbnailService._build_hls_thumbnail_targets(
        [_segment(0, 10.01), _segment(1, 9.99), _segment(2, 5.0)]
    )

    assert expected_count == 3
    assert [(segment.index, offsets) for segment, offsets in targets] == [
        (0, [0, 10]),
        (2, [20]),
    ]


def test_hls_timeline_assigns_exact_boundary_to_next_segment() -> None:
    targets, expected_count = MediaThumbnailService._build_hls_thumbnail_targets(
        [_segment(0), _segment(1)]
    )

    assert expected_count == 2
    assert [(segment.index, offsets) for segment, offsets in targets] == [
        (0, [0]),
        (1, [10]),
    ]


def test_hls_timeline_defers_empty_or_zero_duration_playlist() -> None:
    with pytest.raises(RuntimeError, match="cloud115_hls_segments_empty"):
        MediaThumbnailService._build_hls_thumbnail_targets([])
    with pytest.raises(RuntimeError, match="cloud115_hls_segments_empty"):
        MediaThumbnailService._build_hls_thumbnail_targets([_segment(0, 0.0)])


def test_hls_segment_reader_is_lazy_and_reuses_bound_user_agent() -> None:
    body = b"a" * (256 * 1024)
    seen_user_agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_user_agents.append(request.headers["User-Agent"])
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = Cloud115HlsSegmentReader(
        "https://cdn.example/segment.ts",
        user_agent="SakuraMedia-HLS-Test/1.0",
        chunk_size=4096,
        http_client=client,
    )
    try:
        assert reader.read(100) == b"a" * 100
        assert reader.request_count == 1
        assert 100 <= reader.fetched_bytes < len(body)
        assert seen_user_agents == ["SakuraMedia-HLS-Test/1.0"]
    finally:
        reader.close()
        client.close()


def test_hls_segment_reader_rejects_upstream_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(403))
    )
    reader = Cloud115HlsSegmentReader(
        "https://cdn.example/segment.ts",
        user_agent="SakuraMedia-HLS-Test/1.0",
        http_client=client,
    )
    try:
        with pytest.raises(Cloud115RequestError, match="http 403"):
            reader.read(1)
    finally:
        reader.close()
        client.close()


def test_hls_segment_generation_never_exceeds_three_workers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_decode(segment: VideoSegment, offsets: list[int], webp_dir: Path) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return len(offsets)

    monkeypatch.setattr(
        MediaThumbnailService,
        "_decode_hls_segment_to_webp",
        staticmethod(fake_decode),
    )
    targets = [(_segment(index), [index * 10]) for index in range(8)]

    error = MediaThumbnailService._generate_cloud115_hls_webp(
        targets,
        tmp_path,
        source_label="test",
    )

    assert error is None
    assert peak == 3


def test_hls_segment_decode_limits_network_and_ffmpeg_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeReader:
        fetched_bytes = 0

        def close(self) -> None:
            return None

    class FakeImage:
        @staticmethod
        def save(buffer, **_kwargs) -> None:
            buffer.write(b"webp")

    class FakeFrame:
        is_corrupt = False

        @staticmethod
        def to_image() -> FakeImage:
            return FakeImage()

    class FakeContainer:
        streams = SimpleNamespace(video=[object()])

        @staticmethod
        def decode(_stream):
            return iter([FakeFrame()])

        @staticmethod
        def close() -> None:
            return None

    def fake_reader(_url: str, **kwargs) -> FakeReader:
        captured["reader_kwargs"] = kwargs
        return FakeReader()

    def fake_open(_reader: FakeReader, **kwargs) -> FakeContainer:
        captured["open_kwargs"] = kwargs
        return FakeContainer()

    monkeypatch.setattr(thumbnail_module, "Cloud115HlsSegmentReader", fake_reader)
    monkeypatch.setattr(thumbnail_module.av, "open", fake_open)

    generated_count = MediaThumbnailService._decode_hls_segment_to_webp(
        _segment(0),
        [0],
        tmp_path,
    )

    assert generated_count == 1
    assert captured["reader_kwargs"] == {
        "user_agent": MediaThumbnailService.CLOUD115_THUMBNAIL_UA,
        "chunk_size": 16 * 1024,
    }
    assert captured["open_kwargs"] == {
        "format": "mpegts",
        "options": {"probesize": str(128 * 1024)},
    }
    assert (tmp_path / "0.webp").read_bytes() == b"webp"


def test_local_media_are_parallel_while_cloud115_media_remain_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_barrier = threading.Barrier(2)
    processed_cloud115: list[int] = []
    processed_local: list[int] = []
    monkeypatch.setattr(
        MediaThumbnailService,
        "_pending_media_ids",
        staticmethod(lambda: [1, 2, 3, 4]),
    )
    monkeypatch.setattr(
        MediaThumbnailService,
        "_cloud115_media_ids",
        staticmethod(lambda media_ids: {2, 4}),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        2,
    )

    def process_media(media_id: int) -> dict[str, int]:
        if media_id in {2, 4}:
            processed_cloud115.append(media_id)
        else:
            processed_local.append(media_id)
            local_barrier.wait(timeout=1)
        return {"successful_media": 1, "generated_thumbnails": 1}

    monkeypatch.setattr(
        MediaThumbnailService,
        "_process_media",
        staticmethod(process_media),
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert processed_cloud115 == [2, 4]
    assert set(processed_local) == {1, 3}
    assert stats["successful_media"] == 4
    assert stats["generated_thumbnails"] == 4


def test_thumbnail_dimensions_come_from_generated_webp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "movies" / "sample.webp"
    image_path.parent.mkdir(parents=True)
    PILImage.new("RGB", (640, 360), "black").save(image_path, format="WEBP")
    monkeypatch.setattr(
        MediaThumbnailService,
        "_image_root_path",
        classmethod(lambda cls: tmp_path),
    )

    assert MediaThumbnailService._read_thumbnail_dimensions(
        "movies/sample.webp"
    ) == (640, 360)


def test_video_not_ready_is_classified_as_deferred_system_failure() -> None:
    from src.lib.cloud115 import Cloud115VideoNotReadyError

    assert Cloud115VideoNotReadyError in MediaThumbnailService.CLOUD115_SYSTEM_FAILURES
    assert MediaThumbnailService._minimum_acceptable_thumbnail_count(100) == 85
