"""媒体导入落库 pipeline。

从 ``MediaImportService`` 拆出所有"把已知 movie + 扫描结果写进媒体库"的能力，作为顶层函数暴露。
调用方（``MediaImportService``）负责调度扫描、元数据抓取，然后把结果传进来做搬运、去重、upsert。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Tuple

from loguru import logger

from src.common.media_paths import allocate_next_movie_subtitle_path, movie_subtitle_dir
from src.common.runtime_time import utc_now_for_db
from src.model import Media, MediaLibrary, Movie
from src.service.catalog.movie_subtitle_service import MovieSubtitleService
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.system.resource_task_state_service import ResourceTaskStateService
from src.service.transfers.file_transfer import (
    JAV_LIBRARY_SUBDIR,
    create_version_directory,
    delete_source_files,
    transfer_file,
)
from src.service.transfers.media_source_scanner import (
    ImportTransferMode,
    ScannedSourceFile,
    existing_media_file_exists,
    find_media_by_content_fingerprint,
)
from src.service.transfers.tag_rules import build_media_special_tags


def import_single_scanned_file(
    *,
    file_entry: ScannedSourceFile,
    library: MediaLibrary,
    movie: Movie,
    failure_items: List[Dict[str, str]],
    transfer_mode: ImportTransferMode,
    now_ms: Callable[[], int],
    media_metadata_probe_service: MediaMetadataProbeService,
) -> Tuple[bool, int]:
    """把扫描阶段产出的单个文件搬入媒体库并 upsert Media 记录。"""
    existing_media = find_media_by_content_fingerprint(
        file_entry.content_fingerprint,
        valid=True,
    )
    if existing_media is not None and existing_media_file_exists(
        existing_media,
        source_path=file_entry.path,
        content_fingerprint=file_entry.content_fingerprint,
    ):
        logger.info(
            "Import media duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
            movie.movie_number,
            str(file_entry.path),
            existing_media.id,
            existing_media.path,
            file_entry.content_fingerprint,
        )
        return False, 0

    storage_mode, target_path = _import_single_media_file(
        file_path=file_entry.path,
        library=library,
        movie_number=movie.movie_number,
        transfer_mode=transfer_mode,
        now_ms=now_ms,
    )
    _import_sidecar_subtitle(
        file_entry.subtitle_path,
        target_path,
        movie_number=movie.movie_number,
        transfer_mode=transfer_mode,
    )
    file_size = file_entry.path.stat().st_size
    upsert_media(
        movie=movie,
        library=library,
        target_path=target_path,
        storage_mode=storage_mode,
        content_fingerprint=file_entry.content_fingerprint,
        file_size=file_size,
        special_tag_source_paths=[file_entry.path],
        has_sidecar_subtitle=file_entry.subtitle_path is not None,
        media_metadata_probe_service=media_metadata_probe_service,
    )
    MovieSubtitleService.sync_movie_subtitles(movie)
    logger.info(
        "Import media success movie_number={} source={} target={} storage_mode={}",
        movie.movie_number,
        str(file_entry.path),
        str(target_path),
        storage_mode,
    )
    delete_failed_count = delete_source_files(
        [file_entry.path],
        failure_items,
        transfer_mode=transfer_mode,
    )
    return True, delete_failed_count


def upsert_media(
    *,
    movie: Movie,
    library: MediaLibrary,
    target_path: Path,
    storage_mode: str,
    content_fingerprint: str,
    file_size: int,
    special_tag_source_paths: List[Path],
    has_sidecar_subtitle: bool,
    media_metadata_probe_service: MediaMetadataProbeService,
) -> None:
    """按内容指纹幂等地创建或复活一条 Media 记录，并重置缩略图任务。"""
    if file_size <= 0:
        try:
            file_size = target_path.stat().st_size
        except (FileNotFoundError, OSError):
            file_size = 0
    metadata = media_metadata_probe_service.probe_file(target_path)
    resolution = metadata.resolution
    duration_seconds = metadata.duration_seconds if metadata.duration_seconds > 0 else 0
    invalid_media = find_media_by_content_fingerprint(
        content_fingerprint,
        valid=False,
    )
    effective_video_info = metadata.video_info
    if invalid_media is not None and effective_video_info is None:
        effective_video_info = invalid_media.video_info
    special_tags = build_media_special_tags(
        [str(path) for path in special_tag_source_paths],
        movie.movie_number,
        video_info=effective_video_info,
        has_subtitle=has_sidecar_subtitle,
    )
    if invalid_media is None:
        media = Media.create(
            movie=movie,
            library=library,
            path=str(target_path),
            storage_mode=storage_mode,
            content_fingerprint=content_fingerprint,
            file_size_bytes=file_size,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_info=effective_video_info,
            special_tags=special_tags,
            valid=True,
        )
        _reset_thumbnail_generation_state(media.id)
        return

    invalid_media.movie = movie
    invalid_media.library = library
    invalid_media.path = str(target_path)
    invalid_media.storage_mode = storage_mode
    invalid_media.content_fingerprint = content_fingerprint
    invalid_media.file_size_bytes = file_size
    if resolution is not None:
        invalid_media.resolution = resolution
    if duration_seconds > 0:
        invalid_media.duration_seconds = duration_seconds
    if metadata.video_info is not None:
        invalid_media.video_info = metadata.video_info
    invalid_media.special_tags = special_tags
    invalid_media.valid = True
    invalid_media.updated_at = utc_now_for_db()
    invalid_media.save()
    _reset_thumbnail_generation_state(invalid_media.id)


def _reset_thumbnail_generation_state(media_id: int) -> None:
    # 导入新文件或复活旧媒体后，缩略图任务必须回到全新的待处理状态。
    ResourceTaskStateService.reset_for_requeue(
        MediaThumbnailService.TASK_KEY,
        media_id,
    )


def _import_single_media_file(
    file_path: Path,
    library: MediaLibrary,
    movie_number: str,
    *,
    transfer_mode: ImportTransferMode,
    now_ms: Callable[[], int],
) -> Tuple[str, Path]:
    """为单个媒体文件创建目标版本目录并完成文件传输。"""
    library_root = Path(library.backend_config["root_path"]).expanduser()
    # 复用共享版本目录工具，JAV 实体目录为“库根/jav/番号”，和 videos 平级。
    target_directory = create_version_directory(
        library_root / JAV_LIBRARY_SUBDIR / movie_number,
        now_ms=now_ms(),
    )
    target_filename = f"{movie_number}{file_path.suffix.lower()}"
    target_path = target_directory / target_filename
    storage_mode = transfer_file(file_path, target_path, transfer_mode=transfer_mode)
    return storage_mode, target_path


def prepare_movie_subtitle_target_path(movie_number: str, target_video_path: Path) -> Path:
    """字幕统一落 ``movies/<shard>/<番号>/subtitles/<番号>-<N>.srt``。

    N 由 ``allocate_next_movie_subtitle_path`` 从当前目录已有序号 max + 1 起分配，同一部影片下
    多份字幕（不同版本目录里同名，或同版本目录里 whisperjav 生成的 chinese/plain 两份）都能拿到
    不同序号，避免互相覆盖；也不依赖媒体文件本身是否还在。``target_video_path`` 参数保留是为了
    与上层调用一致，实际不参与命名。
    """
    del target_video_path  # 保留形参一致，实际命名不再依赖版本目录名。
    subtitle_directory = movie_subtitle_dir(movie_number)
    subtitle_directory.mkdir(parents=True, exist_ok=True)
    return allocate_next_movie_subtitle_path(movie_number)


def _import_sidecar_subtitle(
    subtitle_source_path: Path | None,
    target_video_path: Path,
    *,
    movie_number: str,
    transfer_mode: ImportTransferMode,
) -> None:
    # 扫描阶段已按番号匹配好该文件的字幕并缓存到 ScannedSourceFile.subtitle_path，
    # 这里直接复用，不再重复扫盘（避免与 scanner 口径不一致）。
    if subtitle_source_path is None:
        return
    target_subtitle_path = prepare_movie_subtitle_target_path(movie_number, target_video_path)
    transfer_file(subtitle_source_path, target_subtitle_path, transfer_mode=transfer_mode)


