"""媒体导入落库 pipeline。

从 ``MediaImportService`` 拆出所有"把已知 movie + 扫描结果写进媒体库"的能力，作为顶层函数暴露。
调用方（``MediaImportService``）负责调度扫描、元数据抓取，然后把结果传进来做搬运、去重、upsert。
"""

from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Dict, List, Tuple

import ffmpy
from loguru import logger

from src.common.media_import_status import (
    FAILURE_REASON_MERGE_SUBTITLE_SKIPPED_MULTIPLE_SIDECARS,
    make_failure_item,
)
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
    ImportGroup,
    ImportTransferMode,
    ScannedSourceFile,
    build_group_content_fingerprint,
    existing_media_file_exists,
    find_media_by_content_fingerprint,
    find_sidecar_subtitle,
)
from src.service.transfers.tag_rules import build_media_special_tags


IMPORTED_SIDECAR_SUBTITLE_EXTENSION = ".srt"


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
    _import_sidecar_subtitle(file_entry.path, target_path, transfer_mode=transfer_mode)
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


def import_vr_media_group(
    *,
    group: ImportGroup,
    library: MediaLibrary,
    movie: Movie,
    failure_items: List[Dict[str, str]],
    transfer_mode: ImportTransferMode,
    now_ms: Callable[[], int],
    media_metadata_probe_service: MediaMetadataProbeService,
) -> Tuple[bool, int]:
    """把 VR/FC2 多段文件按顺序拼接后落库为一条 Media 记录。"""
    group_fingerprint = build_group_content_fingerprint(
        [file_entry.content_fingerprint for file_entry in group.files]
    )
    existing_media = find_media_by_content_fingerprint(group_fingerprint, valid=True)
    if existing_media is not None and existing_media_file_exists(
        existing_media,
        source_path=group.files[0].path,
        content_fingerprint=group_fingerprint,
    ):
        logger.info(
            "Import VR media group duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
            movie.movie_number,
            ",".join(str(file_entry.path) for file_entry in group.files),
            existing_media.id,
            existing_media.path,
            group_fingerprint,
        )
        return False, 0

    target_directory = create_version_directory(
        Path(library.root_path).expanduser() / JAV_LIBRARY_SUBDIR / movie.movie_number,
        now_ms=now_ms(),
    )
    target_path = target_directory / f"{movie.movie_number}{group.files[0].path.suffix.lower()}"
    merge_media_files(group.files, target_path)

    subtitle_path, multiple_subtitles = select_group_subtitle(group)
    if subtitle_path is not None:
        transfer_file(
            subtitle_path,
            target_path.with_suffix(IMPORTED_SIDECAR_SUBTITLE_EXTENSION),
            transfer_mode=transfer_mode,
        )
    elif multiple_subtitles:
        failure_items.append(
            make_failure_item(group.files[0].path, FAILURE_REASON_MERGE_SUBTITLE_SKIPPED_MULTIPLE_SIDECARS)
        )

    upsert_media(
        movie=movie,
        library=library,
        target_path=target_path,
        storage_mode="concat",
        content_fingerprint=group_fingerprint,
        file_size=target_path.stat().st_size,
        special_tag_source_paths=[file_entry.path for file_entry in group.files],
        has_sidecar_subtitle=subtitle_path is not None,
        media_metadata_probe_service=media_metadata_probe_service,
    )
    MovieSubtitleService.sync_movie_subtitles(movie)
    logger.info(
        "Import VR media group success movie_number={} sources={} target={} content_fingerprint={}",
        movie.movie_number,
        ",".join(str(file_entry.path) for file_entry in group.files),
        str(target_path),
        group_fingerprint,
    )
    delete_failed_count = delete_source_files(
        [file_entry.path for file_entry in group.files],
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
    library_root = Path(library.root_path).expanduser()
    # 复用共享版本目录工具，JAV 实体目录为“库根/jav/番号”，和 videos 平级。
    target_directory = create_version_directory(
        library_root / JAV_LIBRARY_SUBDIR / movie_number,
        now_ms=now_ms(),
    )
    target_filename = f"{movie_number}{file_path.suffix.lower()}"
    target_path = target_directory / target_filename
    storage_mode = transfer_file(file_path, target_path, transfer_mode=transfer_mode)
    return storage_mode, target_path


def _import_sidecar_subtitle(
    source_video_path: Path,
    target_video_path: Path,
    *,
    transfer_mode: ImportTransferMode,
) -> None:
    subtitle_path = find_sidecar_subtitle(source_video_path)
    if subtitle_path is None:
        return
    target_subtitle_path = target_video_path.with_suffix(IMPORTED_SIDECAR_SUBTITLE_EXTENSION)
    transfer_file(subtitle_path, target_subtitle_path, transfer_mode=transfer_mode)


def select_group_subtitle(group: ImportGroup) -> Tuple[Path | None, bool]:
    """从 VR 分段组里挑一份可复用的字幕；多份字幕直接放弃合并。"""
    subtitles = []
    for file_entry in group.files:
        if file_entry.subtitle_path is None:
            continue
        if file_entry.subtitle_path not in subtitles:
            subtitles.append(file_entry.subtitle_path)
    if len(subtitles) == 1:
        return subtitles[0], False
    if len(subtitles) > 1:
        return None, True
    return None, False


def merge_media_files(files: List[ScannedSourceFile], target_path: Path) -> None:
    """按文件名顺序调 ffmpeg concat demuxer 无损拼接为单个视频。"""
    ordered_files = sorted(files, key=lambda item: item.path.name)
    with NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as temp_file:
        temp_file.write("\n".join([f"file '{str(file_entry.path)}'" for file_entry in ordered_files]))
        temp_file_path = Path(temp_file.name)

    try:
        ffmpeg = ffmpy.FFmpeg(
            global_options=["-f", "concat", "-safe", "0"],
            inputs={str(temp_file_path): None},
            outputs={str(target_path): ["-c", "copy"]},
        )
        ffmpeg.run()
        output_size = target_path.stat().st_size if target_path.exists() else 0
        total_input_size = sum(file_entry.path.stat().st_size for file_entry in ordered_files)
        # 拼接产物明显偏小说明 ffmpeg 只处理了一段或者输出被截断，主动失败避免误落库。
        if (
            output_size <= 0
            or output_size < max(file_entry.path.stat().st_size for file_entry in ordered_files)
            or output_size < int(total_input_size * 0.8)
        ):
            raise RuntimeError("merged_file_size_invalid")
    except Exception:
        if target_path.exists():
            target_path.unlink()
        raise
    finally:
        if temp_file_path.exists():
            temp_file_path.unlink()
