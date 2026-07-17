"""115 官方 HLS 清晰度解析与短期缓存。"""

from __future__ import annotations

import time

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


class Cloud115HlsService:
    """按播放器 UA 解析最高码率 HLS，并为 ``/stream`` 提供内部派发地址。"""

    _HLS_TTL_SECONDS = 10 * 60
    _highest_variant_cache: dict[tuple[int, str, str], tuple[str, float]] = {}

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
            for key, (_, expires_at) in cls._highest_variant_cache.items()
            if expires_at <= now
        ]:
            cls._highest_variant_cache.pop(stale_key, None)

    @classmethod
    async def resolve_highest_variant_url(
        cls,
        media: Media,
        *,
        user_agent: str,
    ) -> str:
        """按播放器真实 UA 签发并返回最高码率的 115 variant m3u8 URL。"""
        pickcode = cls._require_pickcode(media)
        cache_key = (media.id, pickcode, user_agent)
        now = time.monotonic()
        cached = cls._highest_variant_cache.get(cache_key)
        if cached is not None:
            url, expires_at = cached
            if expires_at > now:
                logger.debug(
                    "cloud115 highest hls variant cache hit media_id={} ua={!r}",
                    media.id,
                    user_agent,
                )
                return url
            cls._highest_variant_cache.pop(cache_key, None)

        cls._cleanup_expired_cache(now)
        video_info = await cls._get_video_info(
            media,
            pickcode,
            user_agent=user_agent,
        )
        if not video_info.definitions:
            raise ApiError(
                502,
                "cloud115_hls_unavailable",
                "115 未返回可用的 HLS 清晰度",
                {"media_id": media.id},
            )

        # 只做自动派发，不对外暴露清晰度选择；同码率时保持 master playlist 原始顺序。
        highest = max(video_info.definitions, key=lambda item: item.bandwidth)
        cls._highest_variant_cache[cache_key] = (
            highest.m3u8_url,
            now + cls._HLS_TTL_SECONDS,
        )
        return highest.m3u8_url
