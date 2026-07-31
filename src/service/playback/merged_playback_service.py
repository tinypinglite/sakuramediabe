"""影片多分段「合并播放」编排：定位本地分段、规格门槛、构建并缓存虚拟合并布局。

对外提供 [MergedPlaybackService.build_for_movie]，供 router 生成合并流 Range 响应。
"""

from __future__ import annotations

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


class MergedPlaybackService:
    """合并播放布局的构建与进程内缓存。"""

    _CACHE_TTL_SECONDS = 300
    # key = ((media_id, size, mtime_ns), ...) -> (built_at, layout)
    _cache: dict[tuple[tuple[int, int, int], ...], tuple[float, MergedLayout]] = {}

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
        now = time.time()
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
        cls._cleanup_cache()
        cached = cls._cache.get(key)
        if cached is not None and time.time() - cached[0] <= cls._CACHE_TTL_SECONDS:
            return cached[1]

        try:
            parts = [parse_file(str(path)) for _media_id, path in entries]
            layout = build_merged_layout(parts)
        except Mp4MergeError as exc:
            raise ApiError(422, exc.error_code, exc.message) from exc
        except OSError as exc:
            raise ApiError(422, "merged_mp4_unsupported", "合并播放读取分段失败") from exc

        cls._cache[key] = (time.time(), layout)
        return layout

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()
