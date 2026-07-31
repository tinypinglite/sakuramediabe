"""115 HLS 全量代理：构建单播/合播 VOD m3u8 与全局分段索引。

背景：http 部署下把播放器 302 到 https 的 115 CDN 时，ExoPlayer 系播放器默认不跟随
跨协议跳转（http->https），且拿到的 m3u8 若不经 `.m3u8` 后缀也会被按 progressive
处理。本服务把 115 HLS 的 manifest 与 TS 分段全部经后端转发（全量代理）：播放器只
面对后端的 http 地址，无重定向、无跨协议，UA 绑定由后端统一保证。

对外提供：
- [Cloud115HlsProxyService.build_merged_layout]：校验 + 解析各分段最高变体 + 构建
  全局分段索引（含 DISCONTINUITY 边界），带进程内缓存。
- [Cloud115HlsProxyService.render_playlist]：渲染单播/合播 VOD playlist。
- [Cloud115HlsProxyService.proxy_segment]：用绑定 UA 拉 115 CDN 分段并转发字节。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
import peewee
from fastapi.responses import StreamingResponse

from src.api.exception.errors import ApiError
from src.model import Media, MediaLibrary
from src.service.playback import MediaService
from src.service.playback.cloud115_hls_service import Cloud115HlsService

# 单条 TS 分片拉取超时：HLS 播放器并发预取若干分段，超时过长会拖住线程池。
_SEGMENT_FETCH_TIMEOUT_SECONDS = 60.0
_SEGMENT_RELAY_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class HlsSegment:
    media_id: int
    local_index: int
    duration_seconds: float
    url: str


@dataclass(frozen=True)
class MergedHlsLayout:
    media_ids: tuple[int, ...]
    segments: tuple[HlsSegment, ...]
    # 需要在这些全局索引前插入 #EXT-X-DISCONTINUITY（除首个媒体外的每个媒体首段）。
    discontinuity_indexes: frozenset[int]
    total_duration: float
    target_duration: int


class Cloud115HlsProxyService:
    # UA 绑定链：/stream.m3u8 请求的 UA → get_video_info 绑定 variant/TS → 代理分段时
    # 用同一 UA 消费。缓存键含 UA，避免不同播放器互相复用对方绑定的签名地址。
    _TTL_SECONDS = 10 * 60
    _segments_cache: dict[tuple[int, str], tuple[float, tuple[HlsSegment, ...]]] = {}
    _layout_cache: dict[tuple[tuple[int, ...], str], tuple[float, MergedHlsLayout]] = {}
    _lock = threading.Lock()
    _proxy_http: httpx.AsyncClient | None = None

    # ---- 缓存管理 ----

    @classmethod
    def _cleanup(cls, now: float) -> None:
        stale_segments = [
            key
            for key, (expires_at, _) in cls._segments_cache.items()
            if expires_at <= now
        ]
        for key in stale_segments:
            cls._segments_cache.pop(key, None)
        stale_layouts = [
            key
            for key, (expires_at, _) in cls._layout_cache.items()
            if expires_at <= now
        ]
        for key in stale_layouts:
            cls._layout_cache.pop(key, None)

    # ---- 分段解析 ----

    @classmethod
    async def resolve_media_segments(
        cls, media: Media, user_agent: str
    ) -> tuple[HlsSegment, ...]:
        """解析单个 cloud115 media 最高码率 variant 的 TS 分段（按 UA 绑定）。"""
        cache_key = (media.id, user_agent)
        now = time.monotonic()
        with cls._lock:
            cls._cleanup(now)
            cached = cls._segments_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]

        from src.service.cloud115 import cloud115_client_for

        pickcode = Cloud115HlsService._require_pickcode(media)
        video_info = await Cloud115HlsService._get_video_info(
            media, pickcode, user_agent=user_agent
        )
        if not video_info.definitions:
            raise ApiError(
                502,
                "cloud115_hls_unavailable",
                "115 未返回可用的 HLS 清晰度",
                {"media_id": media.id},
            )
        variant = max(video_info.definitions, key=lambda item: item.bandwidth)
        async with cloud115_client_for(media.library, user_agent=user_agent) as client:
            segments = await client.get_video_segments_for_definition(variant)

        result = tuple(
            HlsSegment(
                media_id=media.id,
                local_index=segment.index,
                duration_seconds=segment.duration_seconds,
                url=segment.url,
            )
            for segment in segments
        )
        with cls._lock:
            cls._segments_cache[cache_key] = (now + cls._TTL_SECONDS, result)
        return result

    # ---- 布局构建 ----

    @classmethod
    async def build_merged_layout(
        cls, media_ids: list[int], user_agent: str
    ) -> MergedHlsLayout:
        """校验并构建合播全局分段索引。

        校验：分段非空、全部存在且有效、全部为 cloud115 库、全部归属同一部影片。
        任一媒体拿不到 HLS 即硬失败（缺 TS 无从拼接），不静默跳过。
        """
        unique_ids = list(dict.fromkeys(media_ids))
        if not unique_ids:
            raise ApiError(
                422, "merged_hls_need_at_least_one", "合并播放至少需要 1 个分段"
            )
        cache_key = (tuple(unique_ids), user_agent)
        now = time.monotonic()
        with cls._lock:
            cls._cleanup(now)
            cached = cls._layout_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]

        medias = list(
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
            .where(Media.id.in_(unique_ids))
        )
        if len(medias) != len(unique_ids):
            raise ApiError(404, "media_not_found", "部分分段不存在")
        media_by_id = {media.id: media for media in medias}
        ordered = [media_by_id[i] for i in unique_ids]

        movie_numbers = {media.movie_number for media in ordered}
        if len(movie_numbers) != 1 or None in movie_numbers:
            raise ApiError(422, "merged_hls_cross_movie", "合并分段必须属于同一部影片")

        for media in ordered:
            if not MediaService.is_cloud115_media(media):
                raise ApiError(
                    422,
                    "merged_hls_not_cloud115",
                    "仅云端(115)分段支持 HLS 合并播放",
                    {"media_id": media.id},
                )
            if not media.valid:
                raise ApiError(
                    422,
                    "merged_hls_invalid_media",
                    "存在失效分段",
                    {"media_id": media.id},
                )

        all_segments: list[HlsSegment] = []
        discontinuity_indexes: set[int] = set()
        for media in ordered:
            if all_segments:
                discontinuity_indexes.add(len(all_segments))
            segments = await cls.resolve_media_segments(media, user_agent)
            if not segments:
                raise ApiError(
                    502,
                    "cloud115_hls_unavailable",
                    "分段缺少 HLS 数据",
                    {"media_id": media.id},
                )
            all_segments.extend(segments)

        layout = MergedHlsLayout(
            media_ids=tuple(unique_ids),
            segments=tuple(all_segments),
            discontinuity_indexes=frozenset(discontinuity_indexes),
            total_duration=sum(segment.duration_seconds for segment in all_segments),
            target_duration=int(
                max(segment.duration_seconds for segment in all_segments)
            ) + 1,
        )
        with cls._lock:
            cls._layout_cache[cache_key] = (now + cls._TTL_SECONDS, layout)
        return layout

    # ---- playlist 渲染 ----

    @classmethod
    def render_playlist(
        cls,
        layout: MergedHlsLayout,
        *,
        media_ids_param: str,
        expires: int,
        signature: str,
    ) -> str:
        """渲染单播/合播 VOD playlist；TS 地址指向后端分段代理路由。

        分段地址统一为 ``/media/hls-segment/{全局索引}``，签名参数随每个分段 URL
        透传，供代理路由校验与索引映射复用。
        """
        query = f"?media_ids={media_ids_param}&expires={expires}&signature={signature}"
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{layout.target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        for index, segment in enumerate(layout.segments):
            if index in layout.discontinuity_indexes:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{segment.duration_seconds:.3f},")
            lines.append(f"/media/hls-segment/{index}.ts{query}")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    # ---- 分段转发 ----

    @classmethod
    def _proxy_client(cls) -> httpx.AsyncClient:
        if cls._proxy_http is None:
            cls._proxy_http = httpx.AsyncClient(
                timeout=_SEGMENT_FETCH_TIMEOUT_SECONDS,
                trust_env=False,
            )
        return cls._proxy_http

    @classmethod
    async def proxy_segment(cls, url: str, user_agent: str) -> StreamingResponse:
        """用绑定 UA 拉 115 CDN 分段并转发字节（200 直出，无重定向）。"""
        client = cls._proxy_client()
        try:
            request = client.build_request(
                "GET", url, headers={"User-Agent": user_agent}
            )
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise ApiError(
                502, "cloud115_upstream_error", "115 分段拉取失败", {"detail": str(exc)}
            ) from exc
        if response.status_code != 200:
            status = response.status_code
            await response.aclose()
            raise ApiError(
                502,
                "cloud115_segment_failed",
                f"115 分段返回 {status}",
                {"status": status},
            )

        async def _relay() -> None:
            try:
                async for chunk in response.aiter_bytes(
                    chunk_size=_SEGMENT_RELAY_CHUNK_SIZE
                ):
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            _relay(),
            media_type=response.headers.get("content-type", "video/mp2t"),
            headers={"Cache-Control": "no-store"},
        )
