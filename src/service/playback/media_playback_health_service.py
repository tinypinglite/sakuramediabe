from __future__ import annotations

import math
import statistics
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Media
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback.provider_helpers import media_handle_for

av = None

SAMPLE_PERCENTS = (50,)
SAMPLE_WINDOW_BEFORE_SECONDS = 1.0
SAMPLE_WINDOW_AFTER_SECONDS = 1.0
MAX_SAMPLE_PACKETS = 20_000
MAX_READ_BYTES = 1024 * 1024
ISO_BMFF_SUFFIXES = frozenset({".3gp", ".f4v", ".m4v", ".mov", ".mp4"})
MAX_BOX_SCAN = 256

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_UNKNOWN = "unknown"

REASON_FILE_MISSING = "file_missing"
REASON_CONTAINER_INVALID = "container_invalid"
REASON_MOOV_MISSING = "moov_missing"
REASON_VIDEO_STREAM_MISSING = "video_stream_missing"
REASON_INTERLEAVE_BAD = "interleave_bad"

HealthStatus = Literal["ok", "invalid", "unknown"]


def _load_av():
    global av
    if av is None:
        try:
            import av as pyav
        except ImportError:  # pragma: no cover - runtime dependency guard
            return None
        av = pyav
    return av


class _DefinitiveContainerError(ValueError):
    pass


def _is_provider_budget_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "budget" in name or "budget" in message or "预算" in message


def _budget_error_health(exc: Exception) -> PlaybackHealth:
    return PlaybackHealth(
        status=STATUS_UNKNOWN,
        detail={
            "error_type": "provider_budget_exceeded",
            "error": str(exc),
        },
    )


class _PyAvReaderAdapter:
    def __init__(self, reader, *, file_size: int) -> None:
        self._reader = reader
        self._file_size = file_size
        self._position = self._reader.seek(0)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 1:
            offset += self._position
        elif whence == 2:
            offset += self._file_size
        self._position = max(0, self._reader.seek(offset))
        return self._position

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > MAX_READ_BYTES:
            size = MAX_READ_BYTES
        data = self._reader.read(size)
        self._position += len(data)
        return data

    def close(self) -> None:
        # 底层 reader 的生命周期由 provider 的上下文管理器负责。
        return None


@dataclass(frozen=True)
class PlaybackHealth:
    status: HealthStatus
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class MediaPlaybackHealthService:
    @staticmethod
    def invalid_item(media: Media, health: dict[str, Any] | None = None) -> dict[str, Any]:
        value = health if health is not None else media.playback_health
        reason = value.get("reason") if isinstance(value, dict) else None
        display_name = (
            media.movie_number
            or media.file_name
            or f"media:{media.id}"
        )
        return {
            "media_id": media.id,
            "display_name": display_name,
            "reason": reason,
        }

    @staticmethod
    def is_invalid(health: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(health, dict)
            and health.get("status") == STATUS_INVALID
        )

    @staticmethod
    def reason(health: dict[str, Any] | None) -> str | None:
        if not isinstance(health, dict):
            return None
        reason = health.get("reason")
        return reason if isinstance(reason, str) and reason else None

    @staticmethod
    def interleave_gap_mib(health: dict[str, Any] | None) -> float | None:
        if not isinstance(health, dict):
            return None
        detail = health.get("detail")
        if not isinstance(detail, dict):
            return None
        value = detail.get("max_gap_mib")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        gap_mib = float(value)
        if not math.isfinite(gap_mib) or gap_mib < 0:
            return None
        return gap_mib

    @staticmethod
    def _read_exact(reader, offset: int, size: int) -> bytes:
        reader.seek(offset)
        data = reader.read(size)
        if len(data) != size:
            raise _DefinitiveContainerError("truncated container read")
        return data

    @classmethod
    def has_moov(cls, reader, file_size: int) -> bool | None:
        """Return None when the box scan cap is hit before finding moov."""
        offset = 0
        scanned = 0
        while offset + 8 <= file_size:
            header = cls._read_exact(reader, offset, 8)
            box_size = int.from_bytes(header[:4], "big")
            box_type = header[4:8].decode("latin1")
            header_size = 8
            if box_size == 1:
                extended = cls._read_exact(reader, offset + 8, 8)
                box_size = int.from_bytes(extended, "big")
                header_size = 16
            elif box_size == 0:
                box_size = file_size - offset
            if box_size < header_size or offset + box_size > file_size:
                raise _DefinitiveContainerError(
                    f"invalid box size {box_size} for {box_type}"
                )
            if box_type == "moov":
                return True
            scanned += 1
            if scanned > MAX_BOX_SCAN:
                return None
            offset += box_size
        return False

    @staticmethod
    def _duration_seconds(container, fallback: int) -> float:
        if fallback > 0:
            return float(fallback)
        duration = getattr(container, "duration", None)
        time_base = getattr(_load_av(), "time_base", None)
        if duration is None or not time_base:
            return 0.0
        try:
            return max(0.0, float(duration) / float(time_base))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    @classmethod
    def sample_interleave(
        cls,
        reader,
        *,
        file_size: int,
        duration_seconds: int,
    ) -> PlaybackHealth:
        pyav = _load_av()
        if pyav is None:
            return PlaybackHealth(
                status=STATUS_UNKNOWN,
                detail={"error_type": "pyav_unavailable"},
            )

        container = None
        try:
            container = pyav.open(
                _PyAvReaderAdapter(
                    reader,
                    file_size=file_size,
                ),
                mode="r",
            )
            video_streams = list(getattr(container.streams, "video", ()) or ())
            audio_streams = list(getattr(container.streams, "audio", ()) or ())
            total_video = len(video_streams)
            total_audio = len(audio_streams)
            if not video_streams:
                return PlaybackHealth(
                    status=STATUS_INVALID,
                    reason=REASON_VIDEO_STREAM_MISSING,
                    detail={
                        "video_stream_count": 0,
                        "audio_stream_count": total_audio,
                    },
                )
            if not audio_streams:
                return PlaybackHealth(
                    status=STATUS_OK,
                    detail={
                        "video_stream_count": total_video,
                        "audio_stream_count": 0,
                        "sample_count": 0,
                    },
                )

            video = video_streams[0]
            audio = audio_streams[0]
            duration = cls._duration_seconds(container, duration_seconds)
            if duration <= 0:
                return PlaybackHealth(
                    status=STATUS_UNKNOWN,
                    detail={
                        "error_type": "duration_unavailable",
                        "video_stream_count": total_video,
                        "audio_stream_count": total_audio,
                    },
                )

            samples: list[dict[str, Any]] = []
            budget_error: Exception | None = None
            for percent in SAMPLE_PERCENTS:
                target = duration * percent / 100.0
                try:
                    container.seek(
                        max(
                            0,
                            int(
                                (target - SAMPLE_WINDOW_BEFORE_SECONDS)
                                * pyav.time_base
                            ),
                        ),
                        backward=True,
                        any_frame=False,
                    )
                except Exception as exc:
                    if _is_provider_budget_error(exc):
                        budget_error = exc
                        break
                    logger.debug(
                        "Playback health seek skipped percent={} detail={}",
                        percent,
                        exc,
                    )
                    continue

                selected: dict[int, tuple[float, int]] = {}
                saw_video = saw_audio = False
                after_target = {video.index: False, audio.index: False}
                inspected_packets = 0
                try:
                    for packet in container.demux(video, audio):
                        inspected_packets += 1
                        if inspected_packets > MAX_SAMPLE_PACKETS:
                            break
                        if packet.stream.index not in (video.index, audio.index):
                            continue
                        if packet.pts is None or packet.time_base is None or packet.pos is None:
                            continue
                        pts = float(packet.pts * packet.time_base)
                        if pts < target - SAMPLE_WINDOW_BEFORE_SECONDS:
                            continue
                        if pts > target + SAMPLE_WINDOW_AFTER_SECONDS:
                            after_target[packet.stream.index] = True
                        else:
                            previous = selected.get(packet.stream.index)
                            if (
                                previous is None
                                or abs(pts - target) < abs(previous[0] - target)
                            ):
                                selected[packet.stream.index] = (
                                    pts,
                                    int(packet.pos),
                                )
                            if packet.stream.index == video.index:
                                saw_video = True
                            else:
                                saw_audio = True
                        if (
                            saw_video
                            and saw_audio
                            and all(after_target.values())
                        ):
                            break
                except Exception as exc:
                    if _is_provider_budget_error(exc):
                        budget_error = exc
                        break
                    logger.debug(
                        "Playback health demux skipped percent={} detail={}",
                        percent,
                        exc,
                    )

                video_packet = selected.get(video.index)
                audio_packet = selected.get(audio.index)
                if video_packet is None or audio_packet is None:
                    continue
                samples.append(
                    {
                        "percent": percent,
                        "target_seconds": round(target, 3),
                        "video_pts": round(video_packet[0], 3),
                        "audio_pts": round(audio_packet[0], 3),
                        "gap_bytes": abs(video_packet[1] - audio_packet[1]),
                    }
                )

            if budget_error is not None:
                return _budget_error_health(budget_error)

            if not samples:
                return PlaybackHealth(
                    status=STATUS_UNKNOWN,
                    detail={
                        "error_type": "no_interleave_samples",
                        "video_stream_count": total_video,
                        "audio_stream_count": total_audio,
                    },
                )

            return cls.health_from_samples(
                samples,
                video_stream_count=total_video,
                audio_stream_count=total_audio,
            )
        except Exception as exc:
            if _is_provider_budget_error(exc):
                return _budget_error_health(exc)
            logger.warning(
                "Playback interleave probe failed media_size={} detail={}",
                file_size,
                exc,
            )
            return PlaybackHealth(
                status=STATUS_UNKNOWN,
                detail={"error_type": type(exc).__name__},
            )
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

    @classmethod
    def health_from_samples(
        cls,
        samples: list[dict[str, Any]],
        *,
        video_stream_count: int,
        audio_stream_count: int,
    ) -> PlaybackHealth:
        gaps = [int(item["gap_bytes"]) for item in samples]
        max_gap = max(gaps)
        max_sample = max(samples, key=lambda item: int(item["gap_bytes"]))
        threshold_mib = float(settings.media.playback_interleave_max_gap_mib)
        threshold_bytes = int(threshold_mib * 1024 * 1024)
        detail = {
            "video_stream_count": video_stream_count,
            "audio_stream_count": audio_stream_count,
            "sample_count": len(samples),
            "max_gap_bytes": max_gap,
            "max_gap_mib": round(max_gap / 1024 / 1024, 3),
            "max_gap_percent": max_sample["percent"],
            "median_gap_mib": round(statistics.median(gaps) / 1024 / 1024, 3),
            "threshold_mib": threshold_mib,
        }
        if max_gap >= threshold_bytes:
            return PlaybackHealth(
                status=STATUS_INVALID,
                reason=REASON_INTERLEAVE_BAD,
                detail=detail,
            )
        return PlaybackHealth(status=STATUS_OK, detail=detail)

    @classmethod
    def probe_reader(
        cls,
        reader,
        *,
        file_name: str,
        file_size: int,
        duration_seconds: int,
    ) -> PlaybackHealth:
        if Path(file_name).suffix.lower() in ISO_BMFF_SUFFIXES:
            try:
                has_moov = cls.has_moov(reader, file_size)
            except _DefinitiveContainerError as exc:
                return PlaybackHealth(
                    status=STATUS_INVALID,
                    reason=REASON_CONTAINER_INVALID,
                    detail={"error": str(exc)},
                )
            if has_moov is None:
                return PlaybackHealth(
                    status=STATUS_UNKNOWN,
                    detail={"error_type": "too_many_boxes_before_moov"},
                )
            if not has_moov:
                return PlaybackHealth(
                    status=STATUS_INVALID,
                    reason=REASON_MOOV_MISSING,
                )

        return cls.sample_interleave(
            reader,
            file_size=file_size,
            duration_seconds=duration_seconds,
        )

    @classmethod
    def probe_media(cls, media: Media, *, storage: Any | None = None) -> PlaybackHealth:
        try:
            provider_storage = storage
            if provider_storage is None:
                provider_storage = MEDIA_PROVIDER_REGISTRY.storage_for(media.library)
            # cover source 是现有契约里唯一不触发 transfer 全量哈希的可 seek 视频源。
            open_source = getattr(provider_storage, "open_cover_source", None)
            if not callable(open_source):
                return PlaybackHealth(
                    status=STATUS_UNKNOWN,
                    detail={"error_type": "unsupported_cover_source"},
                )
            source = open_source(media=media_handle_for(media))
            with ExitStack() as stack:
                if hasattr(source, "__enter__") and hasattr(source, "__exit__"):
                    reader = stack.enter_context(source)
                else:
                    reader = stack.enter_context(closing(source))
                try:
                    actual_size = int(reader.seek(0, 2))
                    reader.seek(0)
                except Exception:
                    actual_size = None
                if actual_size is not None and actual_size != int(media.file_size_bytes):
                    return PlaybackHealth(
                        status=STATUS_UNKNOWN,
                        detail={
                            "error_type": "source_size_mismatch",
                            "expected_size_bytes": int(media.file_size_bytes),
                            "actual_size_bytes": actual_size,
                        },
                    )
                return cls.probe_reader(
                    reader,
                    file_name=media.file_name,
                    file_size=int(media.file_size_bytes),
                    duration_seconds=int(media.duration_seconds or 0),
                )
        except ProviderOperationError as exc:
            if exc.code == "source_not_found":
                return PlaybackHealth(
                    status=STATUS_INVALID,
                    reason=REASON_FILE_MISSING,
                )
            return PlaybackHealth(
                status=STATUS_UNKNOWN,
                detail={
                    "error_type": "provider_error",
                    "provider_code": exc.code,
                },
            )
        except Exception as exc:
            if _is_provider_budget_error(exc):
                return _budget_error_health(exc)
            logger.warning(
                "Playback health probe failed media_id={} detail={}",
                media.id,
                exc,
            )
            return PlaybackHealth(
                status=STATUS_UNKNOWN,
                detail={"error_type": type(exc).__name__},
            )

    @staticmethod
    def write_health(media: Media, health: PlaybackHealth) -> None:
        media.playback_health = health.to_json()
        media.updated_at = utc_now_for_db()
        media.save(only=[Media.playback_health, Media.updated_at])

    @classmethod
    def probe_and_write(
        cls,
        media: Media,
        *,
        storage: Any | None = None,
    ) -> PlaybackHealth:
        health = cls.probe_media(media, storage=storage)
        cls.write_health(media, health)
        return health


def health_missing_file() -> dict[str, Any]:
    return PlaybackHealth(
        status=STATUS_INVALID,
        reason=REASON_FILE_MISSING,
    ).to_json()
