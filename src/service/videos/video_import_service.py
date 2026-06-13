"""非 JAV 视频导入 service：指定目录或单文件，就地索引为 VideoItem + Media。

与 JAV 导入不同：不抓取外部元数据、不解析番号、不搬运文件，仅原地登记，
标题默认取文件名；可在导入时一并关联合集。
"""

from pathlib import Path
from typing import List

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.content_fingerprint import compute_content_fingerprint
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.model import (
    Media,
    MediaLibrary,
    VideoCollection,
    VideoItem,
    get_database,
)
from src.schema.videos.imports import VideoImportRequest, VideoImportResultResource
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.tag_rules import build_media_special_tags
from src.service.videos.video_collection_service import VideoCollectionService


class VideoImportService:
    def __init__(self, media_metadata_probe_service: MediaMetadataProbeService | None = None):
        self.media_metadata_probe_service = media_metadata_probe_service or MediaMetadataProbeService()

    @staticmethod
    def _collect_video_files(source_path: str) -> List[Path]:
        source = Path(source_path).expanduser()
        if not source.exists():
            raise ApiError(404, "import_source_not_found", "Import source not found", {"source_path": source_path})
        if source.is_file():
            if source.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                raise ApiError(
                    422,
                    "import_source_unsupported",
                    "Source file is not a supported video",
                    {"source_path": source_path},
                )
            return [source.resolve()]
        files = [
            path.resolve()
            for path in sorted(source.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        ]
        return files

    @staticmethod
    def _validate_associations(payload: VideoImportRequest) -> MediaLibrary | None:
        library = None
        if payload.library_id is not None:
            library = MediaLibrary.get_or_none(MediaLibrary.id == payload.library_id)
            if library is None:
                raise ApiError(404, "media_library_not_found", "Media library not found", {"library_id": payload.library_id})
        if payload.collection_id is not None:
            if VideoCollection.get_or_none(VideoCollection.id == payload.collection_id) is None:
                raise ApiError(
                    404,
                    "video_collection_not_found",
                    "Video collection not found",
                    {"collection_id": payload.collection_id},
                )
        return library

    def _create_video_for_file(
        self,
        file_path: Path,
        *,
        library: MediaLibrary | None,
        payload: VideoImportRequest,
        fingerprint: str,
    ) -> int:
        # 探测分辨率/时长/编码信息（失败时返回空结果，由后续扫描任务补齐）。
        probe = self.media_metadata_probe_service.probe_file(file_path)
        special_tags = build_media_special_tags(
            [file_path.name],
            "",
            video_info=probe.video_info,
            has_subtitle=False,
        )
        file_size = file_path.stat().st_size
        with get_database().atomic():
            video = VideoItem.create(title=file_path.stem)
            Media.create(
                video_item=video,
                library=library,
                path=str(file_path),
                storage_mode="local",
                resolution=probe.resolution,
                content_fingerprint=fingerprint,
                file_size_bytes=file_size,
                duration_seconds=probe.duration_seconds,
                video_info=probe.video_info,
                special_tags=special_tags,
                valid=True,
            )
        if payload.collection_id is not None:
            VideoCollectionService.add_item(payload.collection_id, video.id)
        return video.id

    def import_from_source(self, payload: VideoImportRequest) -> VideoImportResultResource:
        library = self._validate_associations(payload)
        files = self._collect_video_files(payload.source_path)
        created_ids: List[int] = []
        skipped = 0
        for file_path in files:
            # 路径已登记：快速路径，免去计算指纹直接跳过。
            if Media.get_or_none(Media.path == str(file_path)) is not None:
                skipped += 1
                logger.info("Video import skipped already-indexed path={}", str(file_path))
                continue
            # 内容指纹去重：同一物理内容（拷贝/软链/换挂载点）即便路径不同也视为已导入。
            fingerprint = compute_content_fingerprint(file_path)
            if Media.get_or_none(Media.content_fingerprint == fingerprint) is not None:
                skipped += 1
                logger.info("Video import skipped duplicate fingerprint path={}", str(file_path))
                continue
            created_ids.append(
                self._create_video_for_file(
                    file_path, library=library, payload=payload, fingerprint=fingerprint
                )
            )
        logger.info(
            "Video import finished source={} created={} skipped={}",
            payload.source_path,
            len(created_ids),
            skipped,
        )
        return VideoImportResultResource(
            created_count=len(created_ids),
            skipped_count=skipped,
            video_item_ids=created_ids,
        )
