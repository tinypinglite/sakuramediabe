"""非 JAV 视频导入执行器：扫描本地源并登记 VideoItem + Media。

与 JAV 导入共用一套文件落库语义：
- 文件按硬链接优先 / 复制后删源（cleanup-source）搬入指定媒体库根目录；
- 每个 video_item 的 Media 必须归属一个媒体库（library_id 必填）；
- 导入每个视频时读取**第 0 帧**生成封面；
- 进度经回调写入统一 TaskRun。

不抓取外部元数据、不解析番号、不设标签体系；标题默认取文件名 stem，可在导入时一并关联合集。
"""

import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.content_fingerprint import compute_content_fingerprint
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.media_import_status import (
    FAILURE_REASON_ALREADY_INDEXED_PATH,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
)
from src.common.service_helpers import emit_progress
from src.model import (
    Media,
    MediaLibrary,
    VideoCollection,
    VideoItem,
    get_database,
)
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.media_import import ImportResult
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.downloads.guards.tag_rules import build_media_special_tags
from src.service.transfers.imports.source_scanner import (
    find_media_library_containing_path,
)
from src.service.transfers.shared.file_transfer import (
    VIDEOS_LIBRARY_SUBDIR,
    create_version_directory,
    delete_source_files,
    transfer_file,
)
from src.service.videos.video_collection_service import VideoCollectionService
from src.service.videos.video_cover_service import VideoCoverService

ImportProgressCallback = Callable[[dict[str, object]], None]
SUPPORTED_TRANSFER_MODES = ("auto", "cleanup-source")


class VideoImportService:
    def __init__(
        self,
        media_metadata_probe_service: MediaMetadataProbeService | None = None,
        now_ms: Callable[[], int] | None = None,
    ):
        self.media_metadata_probe_service = media_metadata_probe_service or MediaMetadataProbeService()
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    # ---- 文件扫描与校验 ----

    @staticmethod
    def _collect_video_files(
        source_path: str,
    ) -> list[Path]:
        """扫描源路径下的视频文件。"""
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
            files = [source.resolve()]
        else:
            files = [
                path.resolve()
                for path in sorted(source.rglob("*"))
                if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            ]
        return files

    @staticmethod
    def _require_library(library_id: int | None) -> MediaLibrary:
        # videos 域要求每个 Media 都归属媒体库，library_id 必填。
        if library_id is None:
            raise ApiError(422, "media_library_required", "Media library is required for video import", {})
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "Media library not found", {"library_id": library_id})
        if library.backend != MediaLibraryBackend.LOCAL.value:
            raise ApiError(
                422,
                "media_library_backend_mismatch",
                "Video import requires a local media library",
                {"library_id": library_id, "actual_backend": library.backend},
            )
        return library

    @staticmethod
    def _validate_collection(collection_id: int | None) -> None:
        if collection_id is None:
            return
        if VideoCollection.get_or_none(VideoCollection.id == collection_id) is None:
            raise ApiError(
                404,
                "video_collection_not_found",
                "Video collection not found",
                {"collection_id": collection_id},
            )

    @staticmethod
    def _resolve_dedupe(file_path: Path) -> tuple[str | None, str | None]:
        """两级去重判定，返回 (skip_reason, fingerprint)。

        命中去重时 skip_reason 非空、fingerprint 为 None；未命中时 skip_reason 为 None、
        返回算好的内容指纹供落库复用，避免重复计算。
        """
        # 路径已登记：快速路径，免去计算指纹直接跳过。
        if Media.get_or_none(Media.path == str(file_path)) is not None:
            return FAILURE_REASON_ALREADY_INDEXED_PATH, None
        # 内容指纹去重：同一物理内容（拷贝/软链/换挂载点）即便路径不同也视为已导入。
        fingerprint = compute_content_fingerprint(file_path)
        if Media.get_or_none(Media.content_fingerprint == fingerprint) is not None:
            return FAILURE_REASON_DUPLICATE_FINGERPRINT, None
        return None, fingerprint

    @staticmethod
    def _assert_source_outside_libraries(source_entry: Path) -> None:
        # cleanup-source 会删除源文件，禁止对任何媒体库目录或其子路径执行，避免删到库内文件。
        matched = find_media_library_containing_path(source_entry)
        if matched is not None:
            raise ApiError(
                422,
                "cleanup_source_inside_media_library",
                "cleanup-source 模式不能作用于媒体库目录内",
                {"source_path": str(source_entry), "matched_library_id": matched.id},
            )

    # ---- 单文件落库 ----

    def _create_video_for_file(
        self,
        file_path: Path,
        *,
        library: MediaLibrary,
        transfer_mode: str,
        collection_id: int | None,
        fingerprint: str,
        failure_items: list[dict[str, str]],
    ) -> tuple[int, int]:
        """搬运单个视频文件并登记 VideoItem + Media，返回 (video_id, 删源失败数)。"""
        probe = self.media_metadata_probe_service.probe_file(file_path)
        special_tags = build_media_special_tags(
            [file_path.name],
            "",
            video_info=probe.video_info,
            has_subtitle=False,
        )
        # 先建 VideoItem 拿到 id，目标目录以 id 归类，便于与缩略图/封面落盘路径对齐。
        # 发布时间取容器自身的 creation_time（probe 已解析为 UTC naive），读不到则留空。
        video = VideoItem.create(title=file_path.stem, release_date=probe.creation_time)
        target_path: Path | None = None
        try:
            entity_directory = (
                Path(library.backend_config["root_path"]).expanduser()
                / VIDEOS_LIBRARY_SUBDIR
                / str(video.id)
            )
            target_directory = create_version_directory(entity_directory, now_ms=self.now_ms())
            target_path = target_directory / file_path.name
            storage_mode = transfer_file(file_path, target_path, transfer_mode=transfer_mode)
            file_size = target_path.stat().st_size
            with get_database().atomic():
                Media.create(
                    video_item=video,
                    library=library,
                    path=str(target_path),
                    storage_mode=storage_mode,
                    resolution=probe.resolution,
                    content_fingerprint=fingerprint,
                    file_size_bytes=file_size,
                    duration_seconds=probe.duration_seconds,
                    video_info=probe.video_info,
                    special_tags=special_tags,
                    valid=True,
                )
                # 集合关联纳入同一事务：失败则随 Media 一起回滚，
                # 避免出现已建 Media、源已删、却未入合集的半成品状态。
                if collection_id is not None:
                    VideoCollectionService.add_item(collection_id, video.id)
        except Exception:
            # 落库失败时回滚：清理已搬运的目标文件与占位 VideoItem，避免脏数据残留。
            if target_path is not None and target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    logger.warning("Video import rollback unlink failed target={}", str(target_path))
            try:
                video.delete_instance()
            except Exception:
                logger.warning("Video import rollback delete video failed video_id={}", video.id)
            raise

        # 首帧封面：增益项，失败仅记日志不阻断导入。
        VideoCoverService.generate_cover(video, target_path)
        # 落库（含合集关联）成功后再删源（cleanup-source；删失败仅告警计入失败明细）。
        delete_failed = delete_source_files([file_path], failure_items, transfer_mode=transfer_mode)
        logger.info(
            "Video import file done video_id={} source={} target={} storage_mode={}",
            video.id,
            str(file_path),
            str(target_path),
            storage_mode,
        )
        return video.id, delete_failed

    def import_from_source(
        self,
        source_path: str,
        library_id: int,
        *,
        transfer_mode: str = "auto",
        collection_id: int | None = None,
        progress_callback: ImportProgressCallback | None = None,
    ) -> ImportResult:
        """执行一次整源视频导入，单文件失败只计数并继续。"""
        if transfer_mode not in SUPPORTED_TRANSFER_MODES:
            raise ApiError(422, "invalid_transfer_mode", "无效的导入模式", {"transfer_mode": transfer_mode})

        library = self._require_library(library_id)
        self._validate_collection(collection_id)
        source_entry = Path(source_path).expanduser().resolve()
        if not source_entry.exists():
            raise ApiError(404, "import_source_not_found", "Import source not found", {"source_path": source_path})
        if transfer_mode == "cleanup-source":
            self._assert_source_outside_libraries(source_entry)

        failure_items: list[dict[str, str]] = []
        created_ids: list[int] = []
        imported = 0
        skipped = 0
        failed = 0

        def _summary() -> dict[str, object]:
            return {
                "imported_count": imported,
                "skipped_count": skipped,
                "failed_count": failed,
                "created_video_ids": list(created_ids),
            }

        files = self._collect_video_files(str(source_entry))
        total = len(files)
        emit_progress(
            progress_callback,
            event="scan_complete",
            current=0,
            total=total,
            text="视频文件扫描完成",
            summary_patch=_summary(),
        )

        for index, file_path in enumerate(files, start=1):
            emit_progress(
                progress_callback,
                event="file_started",
                current=index - 1,
                total=total,
                text=f"正在导入 {file_path.name}",
                summary_patch=_summary(),
            )

            skip_reason, fingerprint = self._resolve_dedupe(file_path)
            if skip_reason is not None:
                skipped += 1
                logger.info("Video import skipped path={} reason={}", str(file_path), skip_reason)
            else:
                try:
                    video_id, delete_failed = self._create_video_for_file(
                        file_path,
                        library=library,
                        transfer_mode=transfer_mode,
                        collection_id=collection_id,
                        fingerprint=fingerprint,
                        failure_items=failure_items,
                    )
                    imported += 1
                    failed += delete_failed
                    created_ids.append(video_id)
                except Exception as exc:
                    failed += 1
                    logger.exception("Video import file failed source={} detail={}", str(file_path), exc)

            emit_progress(
                progress_callback,
                event="file_finished",
                current=index,
                total=total,
                text=f"已处理 {index}/{total}",
                summary_patch=_summary(),
            )

        logger.info(
            "Video import finished source={} imported={} skipped={} failed={}",
            str(source_entry),
            imported,
            skipped,
            failed,
        )
        return ImportResult(
            imported_count=imported,
            skipped_count=skipped,
            failed_count=failed,
            created_video_ids=created_ids,
        )
