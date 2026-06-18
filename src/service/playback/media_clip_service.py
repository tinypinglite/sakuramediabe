"""片段 service：在媒体资源上按缩略图区间用 ffmpeg 流复制同步切出独立片段文件。

阅读入口建议从 ``create_clip``、``build_clip_resource``、``load_cover_map`` 开始。
片段是独立资产，与来源 Media 解耦：来源被删除后片段记录与文件仍保留。
"""

import subprocess
from pathlib import Path
from typing import Sequence

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common import build_signed_clip_url, media_clip_root_path
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record, resolve_sort, validate_page
from src.config.config import settings
from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaThumbnail,
)
from src.schema.catalog.actors import ImageResource
from src.schema.common.clip_collections import ClipCollectionSummary
from src.schema.common.pagination import PageResponse
from src.schema.playback.clips import (
    MediaClipCreateRequest,
    MediaClipDetailResource,
    MediaClipResource,
    MediaClipThumbnailResource,
    MediaClipUpdateRequest,
)
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService


class MediaClipService:
    MEDIA_CLIP_SORT_FIELDS = {
        "created_at:desc": [MediaClip.created_at.desc(), MediaClip.id.desc()],
        "created_at:asc": [MediaClip.created_at.asc(), MediaClip.id.asc()],
    }

    # ------------------------------------------------------------------ 基础校验

    @staticmethod
    def _require_media(media_id: int) -> Media:
        return require_record(
            Media, Media.id == media_id,
            error_code="media_not_found",
            error_message="Media not found",
            error_details={"media_id": media_id},
        )

    @staticmethod
    def _require_thumbnail_for_media(media: Media, thumbnail_id: int) -> MediaThumbnail:
        return require_record(
            MediaThumbnail,
            MediaThumbnail.id == thumbnail_id,
            MediaThumbnail.media == media,
            error_code="media_thumbnail_not_found",
            error_message="Media thumbnail not found",
            error_details={"media_id": media.id, "thumbnail_id": thumbnail_id},
            query=MediaThumbnail.select(MediaThumbnail),
        )

    @staticmethod
    def _require_clip(clip_id: int) -> MediaClip:
        return require_record(
            MediaClip, MediaClip.id == clip_id,
            error_code="media_clip_not_found",
            error_message="Media clip not found",
            error_details={"clip_id": clip_id},
        )

    # ------------------------------------------------------------------ 资源构建

    @classmethod
    def clip_resource_fields(cls, clip: MediaClip, cover_image: ImageResource | None = None) -> dict:
        """片段资源公共字段，供片段接口与合集接口复用，内联签名串流 URL。"""
        return dict(
            clip_id=clip.id,
            media_id=clip.media_id,
            movie_number=clip.movie_number,
            start_offset_seconds=clip.start_offset_seconds,
            end_offset_seconds=clip.end_offset_seconds,
            title=clip.title,
            duration_seconds=clip.duration_seconds,
            file_size_bytes=clip.file_size_bytes,
            cover_image=cover_image,
            stream_url=build_signed_clip_url(clip.id),
            created_at=clip.created_at,
        )

    @classmethod
    def build_clip_resource(cls, clip: MediaClip, cover_image: ImageResource | None = None) -> MediaClipResource:
        return MediaClipResource(**cls.clip_resource_fields(clip, cover_image))

    @staticmethod
    def load_cover_map(clips: Sequence[MediaClip]) -> dict[tuple[int, int], ImageResource]:
        """批量解析片段封面（区间首帧缩略图），按 (media_id, start_offset) 建索引，避免 N+1。"""
        pairs = {
            (clip.media_id, clip.start_offset_seconds)
            for clip in clips
            if clip.media_id is not None
        }
        if not pairs:
            return {}
        media_ids = {media_id for media_id, _ in pairs}
        offsets = {offset for _, offset in pairs}
        rows = (
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media.in_(media_ids), MediaThumbnail.offset.in_(offsets))
        )
        cover_map: dict[tuple[int, int], ImageResource] = {}
        for thumbnail in rows:
            key = (thumbnail.media_id, thumbnail.offset)
            # in_ 组合可能多取，按精确 (media, offset) 对回填，每个 key 只取一次。
            if key in pairs and key not in cover_map:
                cover_map[key] = ImageResource.from_attributes_model(thumbnail.image)
        return cover_map

    @classmethod
    def _resolve_single_cover(cls, clip: MediaClip) -> ImageResource | None:
        return cls.load_cover_map([clip]).get((clip.media_id, clip.start_offset_seconds))

    # ------------------------------------------------------------------ 文件切片

    @staticmethod
    def _clip_relative_path(movie_number: str | None, clip_id: int) -> str:
        prefix = movie_number or "_unknown"
        return f"{prefix}/{clip_id}.mp4"

    @staticmethod
    def _cut_clip_file(source_path: Path, target_path: Path, start: int, end: int) -> None:
        """ffmpeg 流复制切片：-ss/-to 放输入端做关键帧快速定位，-c copy 不重编码。

        直接用 subprocess 执行而非 ffmpy，以便用 timeout 兜住坏文件/慢挂载导致的进程卡死；
        超时会 kill 子进程干净回收，避免同步切片无限占住请求线程。
        """
        command = [
            "ffmpeg",
            "-ss", str(start), "-to", str(end), "-i", str(source_path),
            "-c", "copy", "-avoid_negative_ts", "make_zero", "-y", str(target_path),
        ]
        try:
            subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=settings.media.media_clip_ffmpeg_timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("clip_cut_timeout") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("clip_cut_failed") from exc
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise RuntimeError("clip_output_empty")

    @classmethod
    def create_clip(
        cls,
        media_id: int,
        payload: MediaClipCreateRequest,
    ) -> tuple[MediaClipResource, bool]:
        media = cls._require_media(media_id)
        start_thumbnail = cls._require_thumbnail_for_media(media, payload.start_thumbnail_id)
        end_thumbnail = cls._require_thumbnail_for_media(media, payload.end_thumbnail_id)

        start = min(start_thumbnail.offset, end_thumbnail.offset)
        end = max(start_thumbnail.offset, end_thumbnail.offset)
        if start >= end:
            raise ApiError(
                422,
                "media_clip_invalid_range",
                "片段需要选择两个不同的时间点",
                {"start_offset_seconds": start, "end_offset_seconds": end},
            )
        max_duration = settings.media.media_clip_max_duration_seconds
        if end - start > max_duration:
            raise ApiError(
                422,
                "media_clip_too_long",
                "片段时长超过上限",
                {"duration_seconds": end - start, "max_duration_seconds": max_duration},
            )

        # 去重：同一来源媒体的同一区间已存在则幂等返回，不重复切片。
        existing = MediaClip.get_or_none(
            MediaClip.media == media,
            MediaClip.start_offset_seconds == start,
            MediaClip.end_offset_seconds == end,
        )
        if existing is not None:
            return cls.build_clip_resource(existing, cls._resolve_single_cover(existing)), False

        source_path = Path(media.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            raise ApiError(404, "file_not_found", "媒体文件不存在", {"media_id": media.id})

        # 解耦后非 JAV 媒体 movie 为空，读外键原始列（None-safe）；下游 _clip_relative_path 已兜 None。
        movie_number = media.movie_number
        # 先落库拿 id 作为文件名，天然避免跨媒体的文件名冲突。
        try:
            clip = MediaClip.create(
                media=media,
                movie_number=movie_number,
                start_offset_seconds=start,
                end_offset_seconds=end,
                title=payload.title,
                file_path="",
                file_size_bytes=0,
                duration_seconds=0,
            )
        except IntegrityError:
            # 与上方去重判断并发：判重到落库之间已出现同区间片段（唯一约束 (media,start,end) 命中），
            # 重查并幂等返回已有片段，不重复切片，与 existing 分支语义一致。
            existing = MediaClip.get_or_none(
                MediaClip.media == media,
                MediaClip.start_offset_seconds == start,
                MediaClip.end_offset_seconds == end,
            )
            if existing is not None:
                return cls.build_clip_resource(existing, cls._resolve_single_cover(existing)), False
            raise
        relative_path = cls._clip_relative_path(movie_number, clip.id)
        target_path = media_clip_root_path() / relative_path
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            cls._cut_clip_file(source_path, target_path, start, end)
            probe = MediaMetadataProbeService.probe_file(target_path)
            clip.file_path = relative_path
            clip.file_size_bytes = target_path.stat().st_size
            clip.duration_seconds = probe.duration_seconds or (end - start)
            clip.save()
        except Exception as exc:
            # 切片失败：清掉占位记录与半成品文件，保持数据与磁盘一致。
            clip.delete_instance()
            cls._unlink_clip_file(target_path)
            logger.warning("Media clip generation failed media_id={} detail={}", media.id, exc)
            raise ApiError(
                500,
                "media_clip_generation_failed",
                "片段生成失败",
                {"media_id": media.id},
            ) from exc

        return cls.build_clip_resource(clip, cls._resolve_single_cover(clip)), True

    # ------------------------------------------------------------------ 查询

    @classmethod
    def _resolve_media_clip_sort(cls, value: str | None) -> Sequence:
        return resolve_sort(
            value, cls.MEDIA_CLIP_SORT_FIELDS,
            default_key="created_at:desc", error_code="invalid_media_clip_filter",
        )

    @classmethod
    def list_clips(cls, media_id: int) -> list[MediaClipResource]:
        cls._require_media(media_id)
        clips = list(
            MediaClip.select()
            .where(MediaClip.media == media_id)
            .order_by(MediaClip.created_at.desc(), MediaClip.id.desc())
        )
        cover_map = cls.load_cover_map(clips)
        return [
            cls.build_clip_resource(
                clip, cover_map.get((clip.media_id, clip.start_offset_seconds))
            )
            for clip in clips
        ]

    @classmethod
    def list_media_clips(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
    ) -> PageResponse[MediaClipResource]:
        validate_page(page, page_size, error_code="invalid_media_clip_filter")
        order_by = cls._resolve_media_clip_sort(sort)
        total = MediaClip.select().count()
        clips = list(
            MediaClip.select()
            .order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        cover_map = cls.load_cover_map(clips)
        items = [
            cls.build_clip_resource(
                clip, cover_map.get((clip.media_id, clip.start_offset_seconds))
            )
            for clip in clips
        ]
        return PageResponse[MediaClipResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_clip_detail(cls, clip_id: int) -> MediaClipDetailResource:
        clip = cls._require_clip(clip_id)
        cover_image = cls._resolve_single_cover(clip)
        preview_frames = cls._load_preview_frames(clip)
        collections = cls._load_clip_collections(clip)
        return MediaClipDetailResource(
            **cls.clip_resource_fields(clip, cover_image),
            preview_frames=preview_frames,
            collections=collections,
        )

    @staticmethod
    def _load_clip_collections(clip: MediaClip) -> list[ClipCollectionSummary]:
        """该片段所属的合集（按名称排序），供前端「加入合集」选择器回显。"""
        rows = (
            ClipCollection.select(ClipCollection.id, ClipCollection.name)
            .join(ClipCollectionItem)
            .where(ClipCollectionItem.clip == clip.id)
            .order_by(ClipCollection.name.asc(), ClipCollection.id.asc())
        )
        return [ClipCollectionSummary(id=row.id, name=row.name) for row in rows]

    @staticmethod
    def _clip_thumbnail_rows(clip: MediaClip) -> list[MediaThumbnail]:
        """片段区间内的源媒体缩略图（含图像），按 offset 升序；来源 Media 已删时为空。"""
        if clip.media_id is None:
            return []
        return list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(
                MediaThumbnail.media == clip.media_id,
                MediaThumbnail.offset >= clip.start_offset_seconds,
                MediaThumbnail.offset <= clip.end_offset_seconds,
            )
            .order_by(MediaThumbnail.offset.asc())
        )

    @classmethod
    def _load_preview_frames(cls, clip: MediaClip) -> list[ImageResource]:
        return [
            ImageResource.from_attributes_model(thumbnail.image)
            for thumbnail in cls._clip_thumbnail_rows(clip)
        ]

    @classmethod
    def list_clip_thumbnails(cls, clip_id: int) -> list[MediaClipThumbnailResource]:
        clip = cls._require_clip(clip_id)
        # 复用源媒体缩略图，把绝对 offset 重定基为片段自身时间轴（从 0 起），供前端进度条定位跳转。
        return [
            MediaClipThumbnailResource(
                clip_id=clip.id,
                thumbnail_id=thumbnail.id,
                offset_seconds=thumbnail.offset - clip.start_offset_seconds,
                image=ImageResource.from_attributes_model(thumbnail.image),
            )
            for thumbnail in cls._clip_thumbnail_rows(clip)
        ]

    # ------------------------------------------------------------------ 更新 / 删除

    @classmethod
    def update_clip(cls, clip_id: int, payload: MediaClipUpdateRequest) -> MediaClipResource:
        clip = cls._require_clip(clip_id)
        clip.title = payload.title
        # 显式刷新更新时间：TimestampedMixin 不自动维护 updated_at，only 列表里也需有人赋值。
        clip.updated_at = utc_now_for_db()
        clip.save(only=[MediaClip.title, MediaClip.updated_at])
        return cls.build_clip_resource(clip, cls._resolve_single_cover(clip))

    @classmethod
    def delete_clip(cls, clip_id: int) -> None:
        clip = cls._require_clip(clip_id)
        target_path = media_clip_root_path() / clip.file_path if clip.file_path else None
        # 单条删除本身原子，依赖 DB 外键 CASCADE 自动清 ClipCollectionItem，无需再包事务。
        clip.delete_instance()
        if target_path is not None:
            cls._unlink_clip_file(target_path)

    @staticmethod
    def _unlink_clip_file(target_path: Path) -> None:
        try:
            target_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Delete media clip file failed path={} detail={}", str(target_path), exc)
