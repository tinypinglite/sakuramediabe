"""非 JAV 视频首帧封面生成 service。

导入每个视频时读取**第 0 帧**生成封面，写入 ``VideoItem.cover_image``。
封面为增益项：PyAV 缺失或解码失败时只记日志、返回 None，绝不阻断导入主流程。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

try:
    import av
except ImportError:  # pragma: no cover - 由运行环境决定，测试不依赖
    av = None

from src.model import Image, VideoItem, get_database
from src.service.playback.media_thumbnail_service import MediaThumbnailService


class VideoCoverService:
    COVER_FILE_NAME = "0.webp"

    @classmethod
    def _cover_path(cls, video_item_id: int) -> Path:
        # 与缩略图同根目录，按 videos/<id>/cover 归类，便于统一签名 URL 与清理。
        return (
            MediaThumbnailService._image_root_path()
            / "videos"
            / str(video_item_id)
            / "cover"
            / cls.COVER_FILE_NAME
        )

    @classmethod
    def generate_cover(cls, video: VideoItem, video_source: Any) -> Image | None:
        """从本地路径或 seekable file-like 读取第 0 帧；失败返回 None。"""
        if av is None:
            logger.warning("Video cover skipped because pyav is unavailable video_id={}", video.id)
            return None

        if isinstance(video_source, (str, Path)):
            resolved_path = Path(video_source).expanduser().resolve()
            if not resolved_path.exists() or not resolved_path.is_file():
                logger.warning("Video cover skipped because file missing video_id={} path={}", video.id, str(resolved_path))
                return None
            av_source: Any = str(resolved_path)
            source_label = str(resolved_path)
        else:
            av_source = video_source
            source_label = getattr(video_source, "name", None) or type(video_source).__name__

        cover_path = cls._cover_path(video.id)
        container = None
        try:
            container = av.open(av_source)
            if not container.streams.video:
                logger.warning("Video cover skipped because no video stream video_id={}", video.id)
                return None
            stream = container.streams.video[0]
            # 严格取第一个可解码帧（第 0 帧），用户明确要求“首帧”。
            frame = next(container.decode(stream))
            cover_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_image().save(cover_path, format="WEBP", quality=80)
        except Exception as exc:
            logger.warning("Video cover generation failed video_id={} source={} detail={}", video.id, source_label, exc)
            return None
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

        # 封面落库同样是增益项：DB 写（建 Image 行、回写 cover_image）失败也只记日志返回 None，
        # 绝不外抛——否则已成功搬运入库的视频会被导入主流程误判为失败文件。
        try:
            image_root = MediaThumbnailService._image_root_path()
            relative_path = cover_path.relative_to(image_root).as_posix()
            with get_database().atomic():
                image = Image.create(
                    origin=relative_path,
                    small=relative_path,
                    medium=relative_path,
                    large=relative_path,
                )
                video.cover_image = image
                video.save()
        except Exception as exc:
            logger.warning(
                "Video cover persist failed video_id={} path={} detail={}",
                video.id,
                source_label,
                exc,
            )
            return None
        logger.info("Video cover generated video_id={} relative_path={}", video.id, relative_path)
        return image
