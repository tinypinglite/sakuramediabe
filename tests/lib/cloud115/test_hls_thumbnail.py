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
from src.service.playback.thumbnails.backends import cloud115_hls as thumbnail_module
from src.service.playback.thumbnails.backends.cloud115_hls import (
    Cloud115HlsThumbnailBackend,
)
from src.service.playback.thumbnails.contracts import PreparedThumbnailSource
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


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
        Cloud115HlsThumbnailBackend,
        "decode_segment",
        staticmethod(fake_decode),
    )
    targets = [(_segment(index), [index * 10]) for index in range(8)]

    result = Cloud115HlsThumbnailBackend.generate(
        PreparedThumbnailSource(
            source_label="test",
            expected_count=len(targets),
            payload=targets,
        ),
        tmp_path,
    )

    assert result.first_error is None
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

    generated_count = Cloud115HlsThumbnailBackend.decode_segment(
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
    test_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kernel 双泳道（Wave 2）：cloud115 泳道先行串行，本地泳道随后按配置并发。"""
    from src.model import Media, MediaLibrary, Movie

    local_barrier = threading.Barrier(2)
    processed_cloud115: list[int] = []
    processed_local: list[int] = []

    local_library = MediaLibrary.create(
        name="thumb-local", backend="local", backend_config={"root_path": "/library"}
    )
    cloud_library = MediaLibrary.create(
        name="thumb-cloud", backend="cloud115", backend_config={"cookies": "x"}
    )
    media_ids: dict[str, list[int]] = {"local": [], "cloud": []}
    for index, (lane, library) in enumerate(
        (("local", local_library), ("cloud", cloud_library)) * 2
    ):
        movie = Movie.create(
            movie_number=f"THB-{index}", javdb_id=f"thb-{index}", title=f"THB-{index}"
        )
        media = Media.create(
            movie=movie,
            library=library,
            path=f"/library/thb-{index}.mp4",
            valid=True,
            content_fingerprint=f"fp-{index}",
        )
        media_ids[lane].append(media.id)
    cloud_id_set = set(media_ids["cloud"])

    def fake_generate(cls, media) -> int:
        if media.id in cloud_id_set:
            processed_cloud115.append(media.id)
        else:
            processed_local.append(media.id)
            # 两条本地媒体必须同时在飞行中才能通过 barrier：验证本地泳道确实并发。
            local_barrier.wait(timeout=2)
        return 1

    monkeypatch.setattr(
        MediaThumbnailTaskService, "generate_for_media", classmethod(fake_generate)
    )
    monkeypatch.setattr(
        "src.service.playback.thumbnails.task_service.settings.media.max_thumbnail_process_count",
        2,
    )

    class _StubReporter:
        def emit(self, **kwargs) -> None:
            pass

    stats = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_StubReporter())

    # cloud115 泳道先跑完（串行、按 id 顺序），本地泳道才开始。
    assert processed_cloud115 == sorted(cloud_id_set)
    assert set(processed_local) == set(media_ids["local"])
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
