from __future__ import annotations

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger

try:
    import av
except ImportError:  # pragma: no cover
    av = None

from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115CipherError,
    Cloud115HlsSegmentReader,
    Cloud115MembershipRequiredError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    Cloud115VideoNotReadyError,
    VideoDefinition,
    VideoSegment,
)
from src.model import Media
from src.service.cloud115 import cloud115_client_for
from src.service.playback.thumbnails.contracts import (
    PreparedThumbnailSource,
    ThumbnailDeferred,
    ThumbnailGenerationResult,
)


def parse_resolution(resolution: str | None) -> tuple[int | None, int | None]:
    if not resolution:
        return None, None
    parts = resolution.lower().split("x")
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


class Cloud115HlsThumbnailBackend:
    key = "cloud115"
    USER_AGENT = "Mozilla/5.0 SakuraMedia-Thumbnail/1.0"
    SEGMENT_MAX_WORKERS = 3
    STREAM_CHUNK_SIZE = 16 * 1024
    PROBE_SIZE = 128 * 1024
    INTERVAL_SECONDS = 10
    SYSTEM_FAILURES = (
        Cloud115AuthError,
        Cloud115CipherError,
        Cloud115MembershipRequiredError,
        Cloud115RateLimitedError,
        Cloud115RequestError,
        Cloud115VideoNotReadyError,
    )

    @classmethod
    def select_lowest_definition(
        cls, definitions: list[VideoDefinition]
    ) -> VideoDefinition:
        if not definitions:
            raise ThumbnailDeferred("cloud115_hls_definitions_empty")

        def sort_key(definition: VideoDefinition) -> tuple[int, int, int, int]:
            width, height = parse_resolution(definition.resolution)
            if width is None or height is None or width <= 0 or height <= 0:
                return (1, 0, 0, max(0, definition.bandwidth))
            return (0, width * height, height, max(0, definition.bandwidth))

        parseable = [
            definition
            for definition in definitions
            if all(
                value is not None and value > 0
                for value in parse_resolution(definition.resolution)
            )
        ]
        return min(parseable or definitions, key=sort_key)

    @classmethod
    def build_targets(
        cls,
        segments: list[VideoSegment],
        *,
        interval_seconds: int | None = None,
    ) -> tuple[list[tuple[VideoSegment, list[int]]], int]:
        interval = interval_seconds or cls.INTERVAL_SECONDS
        if interval <= 0:
            raise ValueError("interval_seconds must be positive")
        timeline: list[tuple[VideoSegment, float, float]] = []
        cursor = 0.0
        for segment in segments:
            duration = max(0.0, float(segment.duration_seconds))
            if duration <= 0:
                continue
            end = cursor + duration
            timeline.append((segment, cursor, end))
            cursor = end
        if not timeline or cursor <= 0:
            raise ThumbnailDeferred("cloud115_hls_segments_empty")

        grouped: dict[int, tuple[VideoSegment, list[int]]] = {}
        timeline_index = 0
        target = 0
        while target < cursor:
            while timeline_index < len(timeline) - 1 and target >= timeline[timeline_index][2]:
                timeline_index += 1
            segment = timeline[timeline_index][0]
            grouped.setdefault(segment.index, (segment, []))[1].append(target)
            target += interval
        targets = list(grouped.values())
        return targets, sum(len(offsets) for _, offsets in targets)

    @classmethod
    async def _resolve_targets(
        cls, media: Media
    ) -> tuple[list[tuple[VideoSegment, list[int]]], int, str]:
        pickcode = (media.backend_locator or {}).get("pickcode")
        if not pickcode:
            raise RuntimeError("cloud115_locator_missing")
        async with cloud115_client_for(media.library, user_agent=cls.USER_AGENT) as client:
            info = await client.get_video_info(pickcode)
            definition = cls.select_lowest_definition(info.definitions)
            segments = await client.get_video_segments_for_definition(definition)
        targets, expected_count = cls.build_targets(segments)
        label = definition.label or definition.resolution or str(definition.bandwidth)
        return targets, expected_count, f"cloud115-hls:{pickcode}:{label}"

    @classmethod
    def prepare(cls, media: Media) -> PreparedThumbnailSource:
        try:
            targets, expected_count, source_label = asyncio.run(cls._resolve_targets(media))
        except (cls.SYSTEM_FAILURES, ThumbnailDeferred) as exc:
            raise ThumbnailDeferred(str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"cloud115_hls_prepare_failed: {exc}") from exc
        return PreparedThumbnailSource(
            source_label=source_label,
            expected_count=expected_count,
            payload=targets,
        )

    @classmethod
    def decode_segment(cls, segment: VideoSegment, offsets: list[int], webp_dir: Path) -> int:
        if av is None:
            raise RuntimeError("pyav_not_installed")
        reader = Cloud115HlsSegmentReader(
            segment.url,
            user_agent=cls.USER_AGENT,
            chunk_size=cls.STREAM_CHUNK_SIZE,
        )
        container = None
        try:
            container = av.open(
                reader,
                format="mpegts",
                options={"probesize": str(cls.PROBE_SIZE)},
            )
            if not container.streams.video:
                raise RuntimeError(f"hls_video_stream_missing segment={segment.index}")
            stream = container.streams.video[0]
            clean_frame = next(
                (frame for frame in container.decode(stream) if not frame.is_corrupt),
                None,
            )
            if clean_frame is None:
                raise RuntimeError(f"hls_clean_frame_missing segment={segment.index}")
            buffer = io.BytesIO()
            clean_frame.to_image().save(buffer, format="WEBP", quality=80)
            content = buffer.getvalue()
            for offset in offsets:
                (webp_dir / f"{offset}.webp").write_bytes(content)
            logger.debug(
                "Decoded cloud115 HLS segment segment_index={} offsets={} fetched_bytes={}",
                segment.index,
                offsets,
                reader.fetched_bytes,
            )
            return len(offsets)
        finally:
            if container is not None:
                container.close()
            reader.close()

    @classmethod
    def generate(
        cls,
        prepared: PreparedThumbnailSource,
        output_dir: Path,
    ) -> ThumbnailGenerationResult:
        first_error: Exception | None = None
        targets = prepared.payload
        with ThreadPoolExecutor(
            max_workers=cls.SEGMENT_MAX_WORKERS,
            thread_name_prefix="cloud115-hls-thumbnail",
        ) as executor:
            futures = {
                executor.submit(cls.decode_segment, segment, offsets, output_dir): (segment, offsets)
                for segment, offsets in targets
            }
            for future in as_completed(futures):
                segment, offsets = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logger.warning(
                        "Cloud115 HLS thumbnail segment failed source={} segment_index={} offsets={} detail={}",
                        prepared.source_label,
                        segment.index,
                        offsets,
                        exc,
                    )
        return ThumbnailGenerationResult(first_error=first_error)
