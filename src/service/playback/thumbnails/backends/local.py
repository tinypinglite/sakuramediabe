from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

try:
    import av
except ImportError:  # pragma: no cover
    av = None

from src.model import Media
from src.service.playback.thumbnails.contracts import (
    PreparedThumbnailSource,
    ThumbnailBackendUnavailable,
    ThumbnailGenerationResult,
)


class LocalThumbnailBackend:
    key = "local"
    INTERVAL_SECONDS = 10

    @staticmethod
    def ensure_available() -> None:
        if av is None:
            raise ThumbnailBackendUnavailable(
                "PyAV 未安装，无法生成本地媒体缩略图",
                error_code="pyav_not_installed",
            )

    @staticmethod
    def _lower_process_priority() -> None:
        try:
            os.nice(19)
        except (AttributeError, OSError):
            return

    @staticmethod
    def _resolve_duration_seconds(container, stream) -> int:
        stream_duration = getattr(stream, "duration", None)
        stream_time_base = getattr(stream, "time_base", None)
        if stream_duration is not None and stream_time_base:
            duration_seconds = float(stream_duration * stream_time_base)
            if duration_seconds > 0:
                return int(duration_seconds)
        container_duration = getattr(container, "duration", None)
        if container_duration is not None and getattr(av, "time_base", None):
            duration_seconds = float(container_duration / av.time_base)
            if duration_seconds > 0:
                return int(duration_seconds)
        return 0

    @classmethod
    def prepare(cls, media: Media) -> PreparedThumbnailSource:
        cls.ensure_available()
        video_path = Path(media.path).expanduser().resolve()
        if not video_path.exists() or not video_path.is_file():
            raise FileNotFoundError("video_file_missing")
        duration_seconds = media.duration_seconds
        if duration_seconds <= 0 and media.movie_number and media.movie.duration_minutes > 0:
            duration_seconds = media.movie.duration_minutes * 60
        expected_count = (
            max(1, duration_seconds // cls.INTERVAL_SECONDS)
            if duration_seconds > 0
            else 0
        )
        return PreparedThumbnailSource(
            source_label=str(video_path),
            expected_count=expected_count,
            payload=str(video_path),
        )

    @classmethod
    def generate(
        cls,
        prepared: PreparedThumbnailSource,
        output_dir: Path,
    ) -> ThumbnailGenerationResult:
        cls.ensure_available()
        cls._lower_process_priority()
        container = None
        first_error: Exception | None = None
        try:
            container = av.open(prepared.payload)
            if not container.streams.video:
                return ThumbnailGenerationResult(RuntimeError("video_stream_missing"))
            stream = container.streams.video[0]
            duration_seconds = cls._resolve_duration_seconds(container, stream)
            if duration_seconds <= 0:
                return ThumbnailGenerationResult(first_error)
            for offset_seconds in range(0, duration_seconds + 1, cls.INTERVAL_SECONDS):
                try:
                    if offset_seconds == 0:
                        # 容器打开后光标就在起点，无需 seek；避开首帧非关键帧的短 AVI 在 seek(0) 时的 pyav 虚警
                        frame = next(container.decode(stream))
                    else:
                        timestamp = int(offset_seconds / float(stream.time_base))
                        container.seek(timestamp, stream=stream, backward=True, any_frame=False)
                        frame = next(container.decode(stream))
                    frame.to_image().save(
                        output_dir / f"{offset_seconds}.webp",
                        format="WEBP",
                        quality=80,
                    )
                except StopIteration:
                    if first_error is None:
                        first_error = RuntimeError(
                            f"decode_frame_missing offset_seconds={offset_seconds}"
                        )
                    logger.warning(
                        "PyAV frame missing media_path={} offset_seconds={}",
                        prepared.source_label,
                        offset_seconds,
                    )
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logger.warning(
                        "PyAV thumbnail generation skipped offset media_path={} offset_seconds={} detail={}",
                        prepared.source_label,
                        offset_seconds,
                        exc,
                    )
        except Exception as exc:
            if first_error is None:
                first_error = exc
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception as exc:
                    first_error = exc
        return ThumbnailGenerationResult(first_error=first_error)
