"""媒体文件搬运与版本目录的无状态共享工具。

JAV 导入（``MediaImportService``）与非 JAV 视频导入（``VideoImportService``）共用同一套
文件落库语义：硬链接优先 / 复制后删源、按时间戳版本目录防覆盖、cleanup-source 删源。
集中到此处避免两份拷贝。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List

from loguru import logger

from src.common.media_import_status import (
    FAILURE_REASON_SOURCE_DELETE_FAILED,
    make_failure_item,
)


# 媒体库根下按内容分类的子目录名，两者平级：库根/jav/番号/... 和 库根/videos/<id>/...
# 新增此类子目录时也在此处集中声明，避免字符串字面量散落。
JAV_LIBRARY_SUBDIR = "jav"
VIDEOS_LIBRARY_SUBDIR = "videos"


def transfer_file(source_path: Path, target_path: Path, *, transfer_mode: str = "auto") -> str:
    """按导入模式把源文件落到目标路径，返回实际存储模式（hardlink/copy）。"""
    if transfer_mode == "cleanup-source":
        # 搬移模式下先复制，删源由调用方在落库成功后统一执行。
        shutil.copy2(source_path, target_path)
        logger.debug("Transfer copied source={} target={}", str(source_path), str(target_path))
        return "copy"

    # 默认模式优先硬链接，失败时再复制，兼容跨文件系统导入。
    try:
        os.link(source_path, target_path)
        logger.debug("Transfer hardlink source={} target={}", str(source_path), str(target_path))
        return "hardlink"
    except OSError as exc:
        logger.warning(
            "Transfer hardlink failed, fallback to copy source={} target={} detail={}",
            str(source_path),
            str(target_path),
            exc,
        )
        shutil.copy2(source_path, target_path)
        logger.debug("Transfer copied source={} target={}", str(source_path), str(target_path))
        return "copy"


def create_version_directory(entity_directory: Path, *, now_ms: int) -> Path:
    """在实体目录下创建唯一时间戳版本子目录，避免重复导入相互覆盖。

    ``entity_directory`` 是某个媒体实体的根目录（JAV 为 ``库根/jav/番号``，视频为 ``库根/videos/<id>``）。
    """
    entity_directory.mkdir(parents=True, exist_ok=True)

    base_version = str(now_ms)
    version = base_version
    suffix = 1
    while (entity_directory / version).exists():
        version = f"{base_version}-{suffix}"
        suffix += 1

    target_directory = entity_directory / version
    target_directory.mkdir(parents=True, exist_ok=False)
    logger.debug("Version directory created entity_dir={} version_dir={}", str(entity_directory), str(target_directory))
    return target_directory


def delete_source_files(
    source_paths: List[Path],
    failure_items: List[Dict[str, str]],
    *,
    transfer_mode: str,
) -> int:
    """仅在 cleanup-source 模式下删除已成功导入的源文件，返回删除失败数（告警级，不阻断导入）。"""
    if transfer_mode != "cleanup-source":
        return 0

    failed_count = 0
    for source_path in source_paths:
        try:
            source_path.unlink()
            logger.info("Import source file deleted source={}", str(source_path))
        except OSError as exc:
            failed_count += 1
            logger.warning("Import source file delete failed source={} detail={}", str(source_path), exc)
            failure_items.append(
                make_failure_item(source_path, FAILURE_REASON_SOURCE_DELETE_FAILED, str(exc))
            )
    return failed_count
