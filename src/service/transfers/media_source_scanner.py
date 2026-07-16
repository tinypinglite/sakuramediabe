"""媒体源目录扫描工具。

从 ``MediaImportService`` 拆出所有"读文件系统 + 计算指纹 + 库内重复检查"能力，作为顶层函数暴露，
供 JAV 导入（``MediaImportService``）和 videos 域（``VideoImportService``）直接复用。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Dict, List, Literal, Tuple

from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.common import parse_movie_number_from_path
from src.common.content_fingerprint import compute_content_fingerprint
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
# 导入状态/失败原因的取值统一收口到 media_import_status 模块。
from src.common.media_import_status import (
    FAILURE_REASON_FILE_TOO_SMALL,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    make_failure_item,
)
from src.config.config import settings
from src.model import Media, MediaLibrary
from src.model.enums import MediaLibraryBackend


ImportTransferMode = Literal["auto", "cleanup-source"]


class ScannedSourceFile(BaseModel):
    """扫描阶段产出的单个待导入文件。"""

    # 允许 pathlib.Path 直接作为字段类型；frozen 保留原 dataclass 的不可变语义。
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    path: Path
    content_fingerprint: str
    subtitle_path: Path | None = None


class ImportGroup(BaseModel):
    """按番号聚合后的一组待导入文件。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    movie_number: str
    files: List[ScannedSourceFile]
    merge_mode: Literal["single", "vr_concat"]


def parse_movie_number(file_path: str) -> str:
    return parse_movie_number_from_path(file_path)


def _normalize_for_fingerprint(movie_number: str) -> str:
    # 指纹用的归一化只做 strip+upper，不做 common.normalize_movie_number 的
    # 去空格/下划线转横杠/剥离 PPV- 前缀，避免历史已入库指纹语义漂移。
    return movie_number.strip().upper()


def build_content_fingerprint(file_path: Path, movie_number: str) -> str:
    """构建内容指纹，番号作为额外维度混入；算法统一收敛到 common 共享实现。"""
    return compute_content_fingerprint(
        file_path,
        discriminator=_normalize_for_fingerprint(movie_number),
    )


def build_group_content_fingerprint(content_fingerprints: List[str]) -> str:
    hasher = hashlib.sha256()
    hasher.update("\0".join(content_fingerprints).encode("utf-8"))
    return hasher.hexdigest()


def find_media_by_content_fingerprint(content_fingerprint: str, *, valid: bool) -> Media | None:
    """按内容指纹查找最新一条指定有效状态的媒体记录。"""
    query = (
        Media.select()
        .where(
            Media.content_fingerprint == content_fingerprint,
            Media.valid == valid,
        )
        .order_by(Media.id.desc())
    )
    return query.first()


def existing_media_file_exists(
    existing_media: Media,
    *,
    source_path: Path,
    content_fingerprint: str,
) -> bool:
    """只有数据库记录指向的真实文件存在时，才允许把源文件当作重复项清理。"""
    existing_path = Path(existing_media.path).expanduser()
    if existing_path.exists():
        return True
    logger.warning(
        "Import duplicate ignored stale media record source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
        str(source_path),
        existing_media.id,
        existing_media.path,
        content_fingerprint,
    )
    return False


def _local_libraries():
    # 本地路径语义只对 backend=local 成立；cloud115 等云盘库没有 root_path，混进来会 KeyError。
    return MediaLibrary.select().where(MediaLibrary.backend == MediaLibraryBackend.LOCAL.value)


def find_media_library_containing_path(source_entry: Path) -> MediaLibrary | None:
    """查找 source_entry 是否落在任一已配置本地媒体库根目录内。"""
    resolved_source = source_entry.expanduser().resolve()
    for media_library in _local_libraries():
        library_root = Path(media_library.backend_config["root_path"]).expanduser().resolve()
        # cleanup-source 会删除源视频，禁止对任何媒体库目录或其子路径执行。
        if resolved_source == library_root or resolved_source.is_relative_to(library_root):
            return media_library
    return None


def media_library_roots() -> List[Path]:
    """返回当前已配置本地媒体库根目录，供 cleanup-source 扫描排除使用。"""
    roots: List[Path] = []
    for media_library in _local_libraries():
        roots.append(Path(media_library.backend_config["root_path"]).expanduser().resolve())
    return roots


def is_path_under_any_root(path: Path, roots: List[Path]) -> bool:
    resolved_path = path.expanduser().resolve()
    return any(resolved_path == root or resolved_path.is_relative_to(root) for root in roots)


def iter_source_paths(source_entry: Path, *, excluded_library_roots: List[Path]) -> List[Path]:
    """枚举源文件；cleanup-source 递归扫描时跳过所有媒体库目录树。"""
    if source_entry.is_file():
        return [source_entry]
    if not excluded_library_roots:
        return sorted(source_entry.rglob("*"))

    candidate_paths: List[Path] = []
    for root, dirs, files in os.walk(source_entry):
        root_path = Path(root).resolve()
        dirs[:] = sorted(
            [
                directory
                for directory in dirs
                if not is_path_under_any_root(root_path / directory, excluded_library_roots)
            ],
            key=str.lower,
        )
        for filename in sorted(files, key=str.lower):
            candidate_paths.append(root_path / filename)
    return candidate_paths


def find_sidecar_subtitle(source_video_path: Path) -> Path | None:
    """查找与视频同名的 .srt 字幕 sidecar 文件。"""
    source_directory = source_video_path.parent
    source_stem = source_video_path.stem
    for path in sorted(source_directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file():
            continue
        if path.stem != source_stem or path.suffix.lower() != ".srt":
            continue
        return path
    return None


def group_needs_multi_part_merge(movie_number: str, files: List[ScannedSourceFile]) -> bool:
    """判定同一番号的多文件分组是否需要拼接合并为一部影片。

    命中的场景共用 ``merge_mode="vr_concat"`` 通道（落库 ``storage_mode="concat"``）：
    - VR 影片常因时长被拆成多段，需要按文件名顺序拼接还原；
    - FC2（含 FC2-PPV）虽然不是 VR，但分段命名习惯一致，复用同一拼接路径；
    - 番号未带 VR 但文件名含 ``VR`` 字样（如片商命名不规范）也按 VR 处理。
    """
    normalized_number = movie_number.upper()
    if "VR" in normalized_number:
        return True
    if normalized_number.startswith("FC2"):
        return True
    return any("VR" in file_entry.path.name.upper() for file_entry in files)


def scan_source_files(
    source_entry: Path,
    failure_items: List[Dict[str, str]],
    *,
    transfer_mode: ImportTransferMode,
    only_file_set: set[str] | None = None,
    on_duplicate_source_paths=None,
) -> Tuple[Dict[str, ImportGroup], int, int]:
    """扫描源目录，过滤无效文件，并按影片编号聚合待导入媒体。

    ``only_file_set`` 提供时仅保留命中其中绝对路径的文件，用于子集重导。
    ``on_duplicate_source_paths(source_paths)`` 在扫描阶段命中已存在媒体记录时被调用，返回删除失败计数；
    调用方以此在 cleanup-source 模式下删除源重复文件（保留原语义）。
    """
    minimum_size = settings.media.allowed_min_video_file_size
    grouped_candidates: Dict[str, List[ScannedSourceFile]] = {}
    skipped_count = 0
    failed_count = 0
    scanned_count = 0
    media_candidate_count = 0

    logger.info(
        "Import scan start source_path={} media_types={} min_size_bytes={}",
        str(source_entry),
        sorted(list(SUPPORTED_VIDEO_EXTENSIONS)),
        minimum_size,
    )

    excluded_library_roots = (
        media_library_roots() if transfer_mode == "cleanup-source" and source_entry.is_dir() else []
    )
    candidate_paths = iter_source_paths(source_entry, excluded_library_roots=excluded_library_roots)
    for path in candidate_paths:
        if not path.is_file():
            continue
        # 子集重导时跳过不在目标集合内的文件，只处理被显式选中的失败文件。
        if only_file_set is not None and str(path.expanduser().resolve()) not in only_file_set:
            continue
        scanned_count += 1
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        media_candidate_count += 1

        # 小文件通常是样本、字幕或下载残片，直接记为 skipped，不进入后续元数据流程。
        file_size = path.stat().st_size
        if file_size < minimum_size:
            skipped_count += 1
            logger.warning(
                "Import scan skip small file path={} size_bytes={} min_size_bytes={}",
                str(path),
                file_size,
                minimum_size,
            )
            failure_items.append(make_failure_item(path, FAILURE_REASON_FILE_TOO_SMALL))
            continue

        movie_number = parse_movie_number(str(path))
        if not movie_number:
            failed_count += 1
            logger.warning("Import scan failed to parse movie number path={}", str(path))
            failure_items.append(make_failure_item(path, FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND))
            continue

        # 指纹里带上归一化后的番号，既能识别同内容重复文件，也能避免不同影片同尺寸文件误撞。
        content_fingerprint = build_content_fingerprint(path, movie_number)
        existing_media = find_media_by_content_fingerprint(content_fingerprint, valid=True)
        if existing_media is not None and existing_media_file_exists(
            existing_media,
            source_path=path,
            content_fingerprint=content_fingerprint,
        ):
            skipped_count += 1
            logger.info(
                "Import media duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
                movie_number,
                str(path),
                existing_media.id,
                existing_media.path,
                content_fingerprint,
            )
            if on_duplicate_source_paths is not None:
                failed_count += on_duplicate_source_paths([path])
            continue

        subtitle_path = find_sidecar_subtitle(path)
        if movie_number not in grouped_candidates:
            grouped_candidates[movie_number] = []
        grouped_candidates[movie_number].append(
            ScannedSourceFile(
                path=path,
                content_fingerprint=content_fingerprint,
                subtitle_path=subtitle_path,
            )
        )
        logger.info("Import scan grouped file path={} movie_number={}", str(path), movie_number)

    grouped_files: Dict[str, ImportGroup] = {}
    for movie_number, file_entries in grouped_candidates.items():
        original_file_count = len(file_entries)
        deduplicated_entries: List[ScannedSourceFile] = []
        seen_fingerprints: set[str] = set()
        for file_entry in sorted(file_entries, key=lambda item: item.path.name):
            if file_entry.content_fingerprint in seen_fingerprints:
                skipped_count += 1
                logger.info(
                    "Import grouped duplicate ignored movie_number={} source={} content_fingerprint={}",
                    movie_number,
                    str(file_entry.path),
                    file_entry.content_fingerprint,
                )
                continue
            deduplicated_entries.append(file_entry)
            seen_fingerprints.add(file_entry.content_fingerprint)

        merge_mode: Literal["single", "vr_concat"] = "single"
        if original_file_count > 1 and group_needs_multi_part_merge(movie_number, deduplicated_entries):
            merge_mode = "vr_concat"
        grouped_files[movie_number] = ImportGroup(
            movie_number=movie_number,
            files=deduplicated_entries,
            merge_mode=merge_mode,
        )

    logger.info(
        "Import scan summary source_path={} scanned_files={} media_candidates={} grouped_numbers={} skipped={} failed={}",
        str(source_entry),
        scanned_count,
        media_candidate_count,
        len(grouped_files),
        skipped_count,
        failed_count,
    )

    return grouped_files, skipped_count, failed_count
