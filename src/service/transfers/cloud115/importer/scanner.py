"""cloud115 导入管线的源目录扫描 / 分拣 / 分组。

拆自 ``service.py``：整块逻辑没有依赖 service 实例状态，唯一的外部 hook 是进度回调
（用于 scan_started / scan_progress 事件）。抽成模块级 async 函数后 strategies 与
service 都可以直接调用，也让 service.py 从 1663 行降下来。
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from src.common import subtitle_matches_movie_number
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.service_helpers import emit_progress
from src.common.media_import_status import (
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_FILE_TOO_SMALL,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    make_failure_item,
)
from src.config.config import settings
from src.lib.cloud115 import Cloud115Client, DirEntry
from src.model import MediaLibrary
from src.service.transfers.cloud115.importer.common import (
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    collect_cloud115_source_files,
)
from src.service.transfers.cloud115.importer.media_registrar import Cloud115MediaRegistrar
from src.service.transfers.cloud115.importer.types import (
    CloudImportGroup,
    CloudSourceFile,
    CloudSubtitleFile,
)
from src.service.transfers.imports.source_scanner import parse_movie_number_from_scan_path

ImportProgressCallback = Callable[[dict], None]


def _entry_suffix(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _entry_is_video(entry: DirEntry) -> bool:
    return _entry_suffix(entry.name) in SUPPORTED_VIDEO_EXTENSIONS




async def scan_cloud115_source(
    client: Cloud115Client,
    *,
    library: MediaLibrary,
    source_cid: str,
    source_name: str,
    transfer_mode: str,
    only_files: list[str] | None,
    failure_items: list[dict],
    progress_callback: ImportProgressCallback | None = None,
) -> tuple[list[CloudImportGroup], int, int]:
    """枚举源目录树 → 分拣视频/字幕 → 番号识别 → sha1 去重，产出按番号聚合的分组。"""
    minimum_size = settings.media.allowed_min_video_file_size
    skipped_count = 0
    failed_count = 0
    only_set = set(only_files) if only_files is not None else None

    emit_progress(
        progress_callback,
        event="scan_started",
        stage="scan",
        text="正在枚举 115 源目录",
    )
    # 整树枚举 + 只为视频文件解析父目录名：请求数与源目录树的目录总数解耦，
    # 空目录（已导入完成的历史任务目录）完全不会被访问。
    source_files, rel_dirs = await collect_cloud115_source_files(
        client, source_cid, needs_rel_path=_entry_is_video
    )
    emit_progress(
        progress_callback,
        event="scan_progress",
        stage="scan",
        text=f"已枚举 {len(source_files)} 个文件，正在分拣",
    )

    videos: list[CloudSourceFile] = []
    subtitles_by_dir: dict[str, list[CloudSubtitleFile]] = {}
    for entry in source_files:
        suffix = _entry_suffix(entry.name)
        if suffix == ".srt":
            subtitles_by_dir.setdefault(entry.parent_id, []).append(
                CloudSubtitleFile(fid=entry.entry_id, pickcode=entry.pickcode, name=entry.name)
            )
            continue
        if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        rel_dir_parts = rel_dirs[entry.parent_id]
        rel_path = "/".join([*rel_dir_parts, entry.name])
        # 失败重导先按相对路径收窄，再做体积/SHA/番号校验；未选择文件不能污染本次统计。
        if only_set is not None and rel_path not in only_set:
            continue
        if entry.size < minimum_size:
            skipped_count += 1
            failure_items.append(
                make_failure_item(rel_path, FAILURE_REASON_FILE_TOO_SMALL)
            )
            continue
        if not entry.sha1:
            # sha1 是去重与 copy 对账的锚点，缺失时无法安全导入（可重导）。
            failed_count += 1
            failure_items.append(
                make_failure_item(
                    rel_path, FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    "115 未返回文件 sha1，无法对账",
                )
            )
            continue
        videos.append(
            CloudSourceFile(
                fid=entry.entry_id,
                pickcode=entry.pickcode,
                name=entry.name,
                sha1=entry.sha1.upper(),
                size=entry.size,
                play_long=entry.play_long,
                censored=entry.ic == 1,
                rel_dir_parts=rel_dir_parts,
                parent_cid=entry.parent_id,
            )
        )

    # 番号识别前置：配对与分组共用识别结果，避免重复解析。
    # 喂「源目录名/相对路径」，与本地扫描共用同一截断策略（取最后两级，覆盖番号在目录名的情形）。
    for video in videos:
        video.movie_number = parse_movie_number_from_scan_path(
            f"{source_name}/{video.rel_path}"
        )

    # 字幕 sidecar 配对：同父目录 + 字幕文件名解析番号与视频番号一致才算配对。
    # 与本地扫描统一到纯番号匹配；番号识别不出的视频无从比对，跳过配对。
    # 同目录多份字幕命中同一番号（如 .chs/.cht）时，按文件名小写排序取第一份，
    # 与本地 find_sidecar_subtitle 保持一致的确定性，不依赖 115 列目录顺序。
    for video in videos:
        if not video.movie_number:
            continue
        candidates = sorted(
            subtitles_by_dir.get(video.parent_cid, []),
            key=lambda item: item.name.lower(),
        )
        for candidate in candidates:
            if subtitle_matches_movie_number(candidate.name, video.movie_number):
                video.subtitle = candidate
                break

    # 分组：复用上面识别好的番号，番号解析不出的计入失败清单。
    grouped: dict[str, CloudImportGroup] = {}
    seen_sha1: set[str] = set()
    for video in videos:
        movie_number = video.movie_number
        if not movie_number:
            failed_count += 1
            failure_items.append(
                make_failure_item(video.rel_path, FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND)
            )
            continue
        # 批内同 sha1 只导第一个。
        if video.sha1 in seen_sha1:
            skipped_count += 1
            failure_items.append(
                make_failure_item(
                    video.rel_path, FAILURE_REASON_DUPLICATE_FINGERPRINT,
                    "同批次存在相同内容文件",
                )
            )
            continue
        # 库内去重（限本库；sha1: 前缀与本地 sha256 裸 hex 值域天然不相交）。
        existing = Cloud115MediaRegistrar.find_library_media(library, video.sha1, valid=True)
        if (
            existing is not None
            and transfer_mode != CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
        ):
            skipped_count += 1
            failure_items.append(
                make_failure_item(
                    video.rel_path, FAILURE_REASON_DUPLICATE_FINGERPRINT,
                    f"库中已存在相同内容（media_id={existing.id}）",
                )
            )
            continue
        seen_sha1.add(video.sha1)
        grouped.setdefault(movie_number, CloudImportGroup(movie_number=movie_number)).files.append(video)

    logger.info(
        "Cloud115 import scan summary source_cid={} videos={} grouped_numbers={} skipped={} failed={}",
        source_cid, len(videos), len(grouped), skipped_count, failed_count,
    )
    return list(grouped.values()), skipped_count, failed_count
