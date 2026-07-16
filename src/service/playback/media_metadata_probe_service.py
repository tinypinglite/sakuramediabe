from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from src.common.runtime_time import parse_external_datetime

try:
    import av
except ImportError:  # pragma: no cover - exercised by runtime environment, not tests
    av = None


@dataclass(frozen=True)
class MediaMetadataProbeResult:
    resolution: str | None = None
    duration_seconds: int = 0
    video_info: dict[str, Any] | None = None
    # 容器自身携带的发布/创建时间（UTC naive），仅 videos 域导入消费；读不到则为 None。
    creation_time: datetime | None = None


class MediaMetadataProbeService:
    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any, precision: int = 3) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if numeric <= 0:
            return None
        return round(numeric, precision)

    @classmethod
    def _resolve_bit_rate(cls, *candidates: Any) -> int | None:
        for candidate in candidates:
            bit_rate = cls._safe_int(candidate)
            if bit_rate is not None and bit_rate > 0:
                return bit_rate
        return None

    @staticmethod
    def _resolve_profile(stream, codec_context) -> str | None:
        for candidate in (
            getattr(stream, "profile", None),
            getattr(codec_context, "profile", None),
        ):
            if candidate is None:
                continue
            normalized = str(candidate).strip()
            if normalized:
                return normalized
        return None

    @staticmethod
    def _resolve_codec_name(stream, codec_context) -> str | None:
        for candidate in (
            getattr(stream, "codec", None),
            getattr(codec_context, "codec", None),
        ):
            if candidate is None:
                continue
            name = getattr(candidate, "name", None)
            if name:
                return str(name)
        for candidate in (
            getattr(stream, "codec_name", None),
            getattr(codec_context, "name", None),
        ):
            if candidate:
                return str(candidate)
        return None

    @staticmethod
    def _resolve_codec_long_name(stream, codec_context) -> str | None:
        for candidate in (
            getattr(stream, "codec", None),
            getattr(codec_context, "codec", None),
        ):
            if candidate is None:
                continue
            long_name = getattr(candidate, "long_name", None)
            if long_name:
                return str(long_name)
        for candidate in (
            getattr(stream, "codec_long_name", None),
            getattr(codec_context, "codec", None),
        ):
            if candidate is None:
                continue
            long_name = getattr(candidate, "long_name", None)
            if long_name:
                return str(long_name)
        return None

    @classmethod
    def _resolve_stream_dimensions(cls, stream) -> str | None:
        width, height = cls._resolve_dimension_pair(stream)
        if width is None or height is None:
            return None
        return f"{width}x{height}"

    @classmethod
    def _resolve_dimension_pair(cls, stream) -> tuple[int | None, int | None]:
        width = getattr(stream, "width", None)
        height = getattr(stream, "height", None)
        if (not width or not height) and getattr(stream, "codec_context", None) is not None:
            codec_context = stream.codec_context
            width = width or getattr(codec_context, "width", None)
            height = height or getattr(codec_context, "height", None)
        return cls._safe_int(width), cls._safe_int(height)

    @classmethod
    def _resolve_frame_rate(cls, stream) -> float | None:
        for candidate in (
            getattr(stream, "average_rate", None),
            getattr(stream, "guessed_rate", None),
            getattr(stream, "base_rate", None),
        ):
            frame_rate = cls._safe_float(candidate)
            if frame_rate is not None:
                return frame_rate
        return None

    @staticmethod
    def _resolve_pixel_format(stream, codec_context) -> str | None:
        for candidate in (
            getattr(stream, "format", None),
            getattr(codec_context, "format", None),
        ):
            if candidate is None:
                continue
            format_name = getattr(candidate, "name", None)
            if format_name:
                return str(format_name)
        for candidate in (
            getattr(stream, "pix_fmt", None),
            getattr(codec_context, "pix_fmt", None),
        ):
            if candidate:
                return str(candidate)
        return None

    @classmethod
    def _build_video_info(cls, stream) -> dict[str, Any]:
        codec_context = getattr(stream, "codec_context", None)
        width, height = cls._resolve_dimension_pair(stream)
        return {
            "codec_name": cls._resolve_codec_name(stream, codec_context),
            "codec_long_name": cls._resolve_codec_long_name(stream, codec_context),
            "profile": cls._resolve_profile(stream, codec_context),
            "bit_rate": cls._resolve_bit_rate(
                getattr(stream, "bit_rate", None),
                getattr(codec_context, "bit_rate", None),
            ),
            "width": width,
            "height": height,
            "frame_rate": cls._resolve_frame_rate(stream),
            "pixel_format": cls._resolve_pixel_format(stream, codec_context),
        }

    @classmethod
    def _build_audio_info(cls, stream) -> dict[str, Any] | None:
        if stream is None:
            return None
        codec_context = getattr(stream, "codec_context", None)
        layout = getattr(stream, "layout", None) or getattr(codec_context, "layout", None)
        channel_layout = getattr(layout, "name", None) if layout is not None else None
        return {
            "codec_name": cls._resolve_codec_name(stream, codec_context),
            "codec_long_name": cls._resolve_codec_long_name(stream, codec_context),
            "profile": cls._resolve_profile(stream, codec_context),
            "bit_rate": cls._resolve_bit_rate(
                getattr(stream, "bit_rate", None),
                getattr(codec_context, "bit_rate", None),
            ),
            "sample_rate": cls._safe_int(
                getattr(stream, "sample_rate", None) or getattr(codec_context, "sample_rate", None)
            ),
            "channels": cls._safe_int(
                getattr(stream, "channels", None) or getattr(codec_context, "channels", None)
            ),
            "channel_layout": str(channel_layout) if channel_layout else None,
        }

    @classmethod
    def _build_subtitle_info(cls, streams) -> list[dict[str, Any]]:
        subtitle_items: list[dict[str, Any]] = []
        for stream in streams:
            codec_context = getattr(stream, "codec_context", None)
            metadata = getattr(stream, "metadata", None) or {}
            language = metadata.get("language") if isinstance(metadata, dict) else None
            subtitle_items.append(
                {
                    "codec_name": cls._resolve_codec_name(stream, codec_context),
                    "codec_long_name": cls._resolve_codec_long_name(stream, codec_context),
                    "language": str(language) if language else None,
                }
            )
        return subtitle_items

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

    @staticmethod
    def _resolve_creation_time(*metadata_sources: Any) -> datetime | None:
        """从容器/视频流 metadata 的 creation_time 解析发布时间，按入参优先级取首个成功值。

        外部文件的时间字符串格式不可控，单个来源解析失败仅记 debug 并跳到下一来源，
        最终读不到/全部非法时如实返回 None；本方法保证不抛异常，以免污染 probe 的其它字段。
        """
        for metadata in metadata_sources:
            if not isinstance(metadata, dict):
                continue
            raw_value = metadata.get("creation_time")
            if raw_value is None or raw_value == "":
                continue
            try:
                creation_time = parse_external_datetime(raw_value)
            except (ValueError, TypeError) as exc:
                logger.debug("Probe creation_time parse failed value={} detail={}", raw_value, exc)
                continue
            if creation_time is not None:
                return creation_time
        return None

    @classmethod
    def _build_container_info(
        cls,
        *,
        container,
        file_size_bytes: int,
        duration_seconds: int,
    ) -> dict[str, Any]:
        container_format = getattr(container, "format", None)
        format_name = getattr(container_format, "name", None) if container_format is not None else None
        return {
            "format_name": str(format_name) if format_name else None,
            "duration_seconds": duration_seconds if duration_seconds > 0 else None,
            "bit_rate": cls._resolve_bit_rate(getattr(container, "bit_rate", None)),
            "size_bytes": file_size_bytes if file_size_bytes > 0 else None,
        }

    @classmethod
    def probe_file(cls, file_path: Path | str) -> MediaMetadataProbeResult:
        path = Path(file_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            return MediaMetadataProbeResult()
        try:
            file_size_bytes = path.stat().st_size
        except OSError:
            return MediaMetadataProbeResult()

        return cls.probe_source(
            str(path),
            file_size_bytes=file_size_bytes,
            source_label=str(path),
        )

    @classmethod
    def probe_source(
        cls,
        source: Any,
        *,
        file_size_bytes: int,
        source_label: str = "<media-source>",
    ) -> MediaMetadataProbeResult:
        """探测路径或 seekable file-like，供本地文件与远端 Range reader 共用。"""

        if av is None:
            logger.warning(
                "Media metadata probe skipped because pyav is unavailable source={}",
                source_label,
            )
            return MediaMetadataProbeResult()

        container = None
        try:
            container = av.open(source)
            if not container.streams.video:
                return MediaMetadataProbeResult()

            stream = container.streams.video[0]
            resolution = cls._resolve_stream_dimensions(stream)
            duration_seconds = cls._resolve_duration_seconds(container, stream)
            # 容器 metadata 优先，其次视频流 metadata；二者都没有则发布时间留空。
            creation_time = cls._resolve_creation_time(
                getattr(container, "metadata", None),
                getattr(stream, "metadata", None),
            )
            video_info = {
                "container": cls._build_container_info(
                    container=container,
                    file_size_bytes=file_size_bytes,
                    duration_seconds=duration_seconds,
                ),
                "video": cls._build_video_info(stream),
                "audio": cls._build_audio_info(getattr(container.streams, "audio", [None])[0])
                if getattr(container.streams, "audio", [])
                else None,
                "subtitles": cls._build_subtitle_info(getattr(container.streams, "subtitles", [])),
            }
            return MediaMetadataProbeResult(
                resolution=resolution,
                duration_seconds=duration_seconds,
                video_info=video_info,
                creation_time=creation_time,
            )
        except Exception as exc:
            logger.warning("Media metadata probe failed source={} detail={}", source_label, exc)
            return MediaMetadataProbeResult()
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
