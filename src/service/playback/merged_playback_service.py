"""影片多分段「合并播放」编排：定位本地分段、规格门槛、构建并缓存虚拟合并布局。

对外提供 [MergedPlaybackService.build_for_movie]，供 router 生成合并流 Range 响应。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from src.api.exception.errors import ApiError
from src.model import Media
from src.service.playback import MediaService
from src.service.playback.merged_mp4 import (
    MergedLayout,
    Mp4MergeError,
    build_merged_layout,
    parse_file,
)


class _InFlightBuild:
    """某个 key 正在进行的合并构建记录。

    并发请求见同一 key 有构建在进行时，等待其完成并复用结果（或复现其错误），
    避免多个线程同时对同一批大文件重复做高耗时构建而占满 API 线程池。
    """

    __slots__ = ("error", "event", "layout")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.layout: MergedLayout | None = None
        self.error: BaseException | None = None


class MergedPlaybackService:
    """合并播放布局的构建与进程内缓存。"""

    _CACHE_TTL_SECONDS = 300
    # key = ((media_id, size, mtime_ns), ...) -> (built_at, layout)
    _cache: dict[tuple[tuple[int, int, int], ...], tuple[float, MergedLayout]] = {}
    # 正在构建的 key -> 构建记录；与 _cache 同受 _lock 保护。
    _inflight: dict[tuple[tuple[int, int, int], ...], _InFlightBuild] = {}
    _lock = threading.Lock()

    @classmethod
    def _cache_key(
        cls, entries: list[tuple[int, Path]]
    ) -> tuple[tuple[int, int, int], ...]:
        stat_key: list[tuple[int, int, int]] = []
        for media_id, path in entries:
            stat = path.stat()
            stat_key.append((media_id, stat.st_size, stat.st_mtime_ns))
        return tuple(stat_key)

    @classmethod
    def _cleanup_cache(cls) -> None:
        # TTL 判断用单调时钟，避免系统时间回拨导致缓存长期不清理
        now = time.monotonic()
        stale = [
            key
            for key, (built_at, _layout) in cls._cache.items()
            if now - built_at > cls._CACHE_TTL_SECONDS
        ]
        for key in stale:
            cls._cache.pop(key, None)

    @classmethod
    def build_for_media_ids(cls, media_ids: list[int]) -> MergedLayout:
        """按显式指定的分段合并播放。

        校验：分段全部存在、全部本地库（非 cloud115）、文件都在、且**全部归属同一部影片**。
        合并顺序按 ``media_ids`` 传入顺序（去重后）。任一分段不符合即抛 422/404，不静默跳过。
        """
        unique_ids = list(dict.fromkeys(media_ids))
        if len(unique_ids) < 2:
            raise ApiError(
                422,
                "merged_mp4_need_at_least_two",
                "合并播放至少需要 2 个分段",
            )
        medias = list(
            Media.select(Media).where(Media.id.in_(unique_ids))
        )
        if len(medias) != len(unique_ids):
            raise ApiError(404, "media_not_found", "部分分段不存在")

        movie_numbers = {media.movie_number for media in medias}
        if len(movie_numbers) != 1 or None in movie_numbers:
            raise ApiError(
                422,
                "merged_mp4_cross_movie",
                "合并分段必须属于同一部影片",
            )

        # 合并顺序按前端传入的 media_ids 顺序（去重后），不按 Media.id 重排。
        media_by_id = {media.id: media for media in medias}
        ordered = [media_by_id[i] for i in unique_ids]
        entries: list[tuple[int, Path]] = []
        for media in ordered:
            if MediaService.is_cloud115_media(media):
                raise ApiError(
                    422,
                    "merged_mp4_unsupported",
                    "云端(115)分段不支持本地合并播放",
                )
            if not media.path:
                raise ApiError(404, "file_not_found", "分段文件不存在")
            path = Path(media.path).expanduser().resolve()
            if not path.exists() or not path.is_file():
                raise ApiError(404, "file_not_found", "分段文件不存在")
            entries.append((media.id, path))

        key = cls._cache_key(entries)
        with cls._lock:
            cls._cleanup_cache()
            cached = cls._cache.get(key)
            if cached is not None and time.monotonic() - cached[0] <= cls._CACHE_TTL_SECONDS:
                return cached[1]
            inflight = cls._inflight.get(key)
            if inflight is None:
                inflight = _InFlightBuild()
                cls._inflight[key] = inflight
                owner = True
            else:
                owner = False

        if not owner:
            # 同一批分段的构建已在进行，等待其完成并复用结果（或复现其错误）。
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            if inflight.layout is not None:
                return inflight.layout
            raise ApiError(422, "merged_mp4_unsupported", "合并播放构建状态异常")

        try:
            try:
                parts = [parse_file(str(path)) for _media_id, path in entries]
                layout = build_merged_layout(parts)
            except Mp4MergeError as exc:
                raise ApiError(422, exc.error_code, exc.message) from exc
            except OSError as exc:
                raise ApiError(422, "merged_mp4_unsupported", "合并播放读取分段失败") from exc
        except BaseException as exc:
            inflight.error = exc
            raise
        else:
            inflight.layout = layout
            with cls._lock:
                cls._cache[key] = (time.monotonic(), layout)
            return layout
        finally:
            # 先唤醒等待者再摘除 inflight 记录，避免等待者 wait 永不返回。
            inflight.event.set()
            with cls._lock:
                cls._inflight.pop(key, None)

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()
