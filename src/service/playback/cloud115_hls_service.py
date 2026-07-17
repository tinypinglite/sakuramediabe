"""115 官方 HLS 清晰度解析与短期缓存。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from loguru import logger

from src.api.exception.errors import ApiError
from src.lib.cloud115 import (
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115VideoNotReadyError,
    VideoInfo,
)
from src.model import Media
from src.service.playback.cloud115_backend_service import (
    cloud115_client_for,
    map_cloud115_error,
)


@dataclass(frozen=True, slots=True)
class HlsStream:
    quality: str
    resolution: str
    bandwidth: int


class Cloud115HlsService:
    """解析 HLS 清晰度，并按播放器 UA 获取可 302 的 variant 地址。"""

    _HLS_TTL_SECONDS = 10 * 60
    _metadata_cache: dict[tuple[int, str], tuple[list[HlsStream], float]] = {}
    _variant_cache: dict[
        tuple[int, str, str],
        tuple[dict[int, str], float],
    ] = {}

    @staticmethod
    def _require_pickcode(media: Media) -> str:
        from src.service.playback.media_service import MediaService

        if not MediaService.is_cloud115_media(media):
            raise ApiError(
                400,
                "hls_not_cloud115_media",
                "本地媒体不支持 HLS",
                {"media_id": media.id},
            )

        pickcode = (media.backend_locator or {}).get("pickcode")
        if not pickcode:
            raise ApiError(
                404,
                "media_locator_missing",
                "媒体缺少 cloud115 定位信息",
                {"media_id": media.id},
            )
        return pickcode

    @staticmethod
    async def _get_video_info(
        media: Media,
        pickcode: str,
        *,
        user_agent: str | None = None,
    ) -> VideoInfo:
        try:
            async with cloud115_client_for(
                media.library,
                user_agent=user_agent,
            ) as client:
                return await client.get_video_info(pickcode)
        except Cloud115MembershipRequiredError as exc:
            raise ApiError(
                422,
                "cloud115_membership_required",
                "115 HLS 播放需要 VIP 会员",
                {"detail": str(exc)},
            ) from exc
        except Cloud115VideoNotReadyError as exc:
            raise ApiError(
                503,
                "cloud115_video_transcoding",
                "115 视频正在转码，请稍后再试",
                {"detail": str(exc), "file_status": exc.file_status},
                response_headers={"Retry-After": "300"},
            ) from exc
        except Cloud115NotFoundError as exc:
            raise ApiError(
                404,
                "hls_not_video",
                "该 115 文件不是可转码播放的视频",
                {"detail": str(exc)},
            ) from exc
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    @classmethod
    def _cleanup_expired_cache(cls, now: float) -> None:
        for stale_key in [
            key
            for key, (_, expires_at) in cls._metadata_cache.items()
            if expires_at <= now
        ]:
            cls._metadata_cache.pop(stale_key, None)
        for stale_key in [
            key
            for key, (_, expires_at) in cls._variant_cache.items()
            if expires_at <= now
        ]:
            cls._variant_cache.pop(stale_key, None)

    @classmethod
    async def list_hls_streams(cls, media: Media) -> list[HlsStream]:
        """获取清晰度元数据；上游 URL 仅用于解析，不直接暴露给前端。"""
        pickcode = cls._require_pickcode(media)
        cache_key = (media.id, pickcode)
        now = time.monotonic()
        cached = cls._metadata_cache.get(cache_key)
        if cached is not None:
            streams, expires_at = cached
            if expires_at > now:
                logger.debug(
                    "cloud115 hls metadata cache hit media_id={} pickcode={}",
                    media.id,
                    pickcode,
                )
                return streams
            cls._metadata_cache.pop(cache_key, None)

        cls._cleanup_expired_cache(now)
        video_info = await cls._get_video_info(media, pickcode)

        streams = sorted(
            (
                HlsStream(
                    quality=definition.label,
                    resolution=definition.resolution,
                    bandwidth=definition.bandwidth,
                )
                for definition in video_info.definitions
            ),
            key=lambda stream: stream.bandwidth,
            reverse=True,
        )
        cls._metadata_cache[cache_key] = (
            streams,
            now + cls._HLS_TTL_SECONDS,
        )
        return streams

    @classmethod
    async def resolve_hls_variant_url(
        cls,
        media: Media,
        *,
        bandwidth: int,
        user_agent: str,
    ) -> str:
        """按播放器真实 UA 签发并返回指定码率的 115 variant m3u8 URL。"""
        if not user_agent:
            raise ApiError(
                400,
                "hls_user_agent_missing",
                "播放器请求缺少 User-Agent",
                {"media_id": media.id},
            )
        pickcode = cls._require_pickcode(media)
        cache_key = (media.id, pickcode, user_agent)
        now = time.monotonic()
        cached = cls._variant_cache.get(cache_key)
        if cached is not None:
            variants, expires_at = cached
            if expires_at > now:
                url = variants.get(bandwidth)
                if url is not None:
                    logger.debug(
                        "cloud115 hls variant cache hit media_id={} bandwidth={} ua={!r}",
                        media.id,
                        bandwidth,
                        user_agent,
                    )
                    return url
                raise ApiError(
                    404,
                    "hls_stream_not_found",
                    "请求的 HLS 清晰度不存在",
                    {"media_id": media.id, "bandwidth": bandwidth},
                )
            else:
                cls._variant_cache.pop(cache_key, None)

        cls._cleanup_expired_cache(now)
        video_info = await cls._get_video_info(
            media,
            pickcode,
            user_agent=user_agent,
        )
        # 同码率分支按 master playlist 首次出现者为准，与 SDK _pick_variant 契约一致。
        variants: dict[int, str] = {}
        for definition in video_info.definitions:
            variants.setdefault(definition.bandwidth, definition.m3u8_url)
        cls._variant_cache[cache_key] = (
            variants,
            now + cls._HLS_TTL_SECONDS,
        )
        url = variants.get(bandwidth)
        if url is None:
            raise ApiError(
                404,
                "hls_stream_not_found",
                "请求的 HLS 清晰度不存在",
                {"media_id": media.id, "bandwidth": bandwidth},
            )
        return url
