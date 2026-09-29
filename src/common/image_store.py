"""媒体图片字节的统一读取入口：缩略图包优先、单文件兜底。

缩略图包与缩略图目录同级同名（``thumbnails.zip``）。包一旦存在即视为该 media
缩略图的主要存储；包内条目缺失时回退同名单文件，两者都没有时按"文件缺失"
处理（``FileNotFoundError``），与旧版调用方的异常语义保持一致。
"""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from src.common.file_signatures import resolve_image_file_path
from src.common.media_paths import image_pack_relative_path, media_image_root_path


def image_pack_path(relative_path: str) -> Path | None:
    """图片对应的包绝对路径；非可打包路径返回 None（不检查是否存在）。"""
    pack_relative = image_pack_relative_path(relative_path)
    if pack_relative is None:
        return None
    return media_image_root_path() / pack_relative


def read_image_bytes(relative_path: str) -> bytes:
    """读取图片字节：包条目优先，缺失时回退单文件，均不存在抛 FileNotFoundError。"""
    pack_path = image_pack_path(relative_path)
    if pack_path is not None and pack_path.is_file():
        entry_name = PurePosixPath(relative_path.replace("\\", "/")).name
        try:
            with zipfile.ZipFile(pack_path) as archive:
                return archive.read(entry_name)
        except (KeyError, zipfile.BadZipFile):
            # 条目缺失或包损坏属于异常状态：回退单文件，文件也不在则按缺失处理。
            pass
    return resolve_image_file_path(relative_path).read_bytes()


def write_pack(pack_path: Path, entries: Iterable[tuple[str, Path | bytes]]) -> None:
    """写 ZIP_STORED 缩略图包并 fsync；调用方负责临时路径与原子替换。

    缩略图已是压缩格式，包只作容器不压缩；逐条目按给定顺序写入。
    """
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        pack_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for entry_name, source in entries:
            if isinstance(source, bytes):
                archive.writestr(entry_name, source)
            else:
                archive.write(source, arcname=entry_name)
    descriptor = os.open(pack_path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
