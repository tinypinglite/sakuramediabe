"""cloud115 导入公共原语：模式、互斥键、受预算媒体探测与 RangeReader 构造。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from src.lib.cloud115 import Cloud115Client, Cloud115RangeReader, DirEntry
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)

CLOUD115_TRANSFER_MODE_COPY = "copy"
CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE = "cleanup-source"
CLOUD115_TRANSFER_MODE_LEGACY_MOVE = "move"

CLOUD115_METADATA_PROBE_MAX_BYTES = 64 * 1024 * 1024
CLOUD115_METADATA_PROBE_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Metadata/1.0"
CLOUD115_COVER_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Cover/1.0"

# 相对路径段编码到扁平受管目录文件名时使用的公共规则。
CLOUD_NAME_SEPARATOR = "＿"
CLOUD_NAME_MAX_LENGTH = 200


def normalize_cloud115_transfer_mode(
    transfer_mode: str,
    *,
    allow_legacy_move: bool = True,
) -> str:
    if transfer_mode == CLOUD115_TRANSFER_MODE_LEGACY_MOVE and allow_legacy_move:
        return CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
    if transfer_mode in (
        CLOUD115_TRANSFER_MODE_COPY,
        CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    ):
        return transfer_mode
    raise ValueError("invalid_transfer_mode")


def cloud115_import_mutex_key(library_id: int) -> str:
    """JAV/videos 对同一 cloud115 库共享唯一写锁。"""
    return f"media_import:cloud115:{library_id}"


def encode_cloud_file_name(
    rel_dir_parts: tuple[str, ...],
    file_name: str,
    *,
    max_length: int = CLOUD_NAME_MAX_LENGTH,
) -> str:
    """把源内相对路径编码进扁平目标文件名，并保留文件名与扩展名。"""
    parts = [part for part in rel_dir_parts if part]
    name = CLOUD_NAME_SEPARATOR.join([*parts, file_name]) if parts else file_name
    while len(name) > max_length and parts:
        parts.pop(0)
        name = CLOUD_NAME_SEPARATOR.join([*parts, file_name]) if parts else file_name
    if len(name) > max_length:
        stem, dot, suffix = file_name.rpartition(".")
        if dot:
            keep = max(1, max_length - len(suffix) - 1)
            name = f"{stem[:keep]}.{suffix}"
        else:
            name = file_name[:max_length]
    return name


async def build_cloud115_dir_map(
    client: Cloud115Client, source_cid: str
) -> Dict[str, tuple[str, str]]:
    """BFS 枚举源目录树，返回 cid -> (名称, 父 cid)。"""
    dir_map: Dict[str, tuple[str, str]] = {}
    queue = [source_cid]
    while queue:
        cid = queue.pop(0)
        offset = 0
        while True:
            entries, total = await client.list_dir(cid, offset=offset, limit=1150)
            for entry in entries:
                if entry.is_dir:
                    dir_map[entry.entry_id] = (entry.name, cid)
                    queue.append(entry.entry_id)
            offset += len(entries)
            if not entries or offset >= total:
                break
    return dir_map


def cloud115_rel_dir_parts(
    parent_cid: str,
    dir_map: Dict[str, tuple[str, str]],
    source_cid: str,
) -> tuple[str, ...]:
    parts: List[str] = []
    current = parent_cid
    while current != source_cid:
        mapped = dir_map.get(current)
        if mapped is None:
            break
        name, parent = mapped
        parts.append(name)
        current = parent
    return tuple(reversed(parts))


async def list_cloud115_target_files(
    client: Cloud115Client, target_cid: str
) -> Dict[str, List[DirEntry]]:
    """列扁平目标目录并按大写 SHA1 建索引。"""
    by_sha1: Dict[str, List[DirEntry]] = {}
    offset = 0
    while True:
        entries, total = await client.list_dir(target_cid, offset=offset, limit=1150)
        for entry in entries:
            if not entry.is_dir and entry.sha1:
                by_sha1.setdefault(entry.sha1.upper(), []).append(entry)
        offset += len(entries)
        if not entries or offset >= total:
            break
    return by_sha1


def resolve_cloud115_copied_entry(
    target_entries_by_sha1: Dict[str, List[DirEntry]],
    source_file: Any,
    encoded_name: str,
) -> DirEntry | None:
    """按 SHA1 对账复制结果，优先匹配源文件名或编码后的目标名。"""
    candidates = target_entries_by_sha1.get(source_file.sha1) or []
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.name in (source_file.name, encoded_name):
            return candidate
    return candidates[0]


async def verify_cloud115_renamed_file(
    client: Cloud115Client,
    fid: str,
    expected_name: str,
) -> None:
    """按 FID 查询并等待 115 改名结果可见。"""
    last_detail = ""
    for delay in (0.0, 0.5, 1.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            meta = await client.file_info(fid)
            if meta.name == expected_name:
                return
            last_detail = f"actual_name={meta.name!r}"
        except Exception as exc:
            last_detail = str(exc)
    raise RuntimeError(
        f"rename did not become visible for fid={fid}, expected={expected_name!r}, {last_detail}"
    )


async def open_cloud115_range_reader(
    client: Cloud115Client,
    *,
    pickcode: str,
    file_size_bytes: int,
    user_agent: str,
    max_fetched_bytes: int | None = CLOUD115_METADATA_PROBE_MAX_BYTES,
) -> Cloud115RangeReader:
    direct = await client.get_download_url(pickcode, user_agent)
    effective_size = direct.file_size or file_size_bytes
    return Cloud115RangeReader(
        direct.url,
        user_agent=direct.user_agent,
        file_size=effective_size,
        max_fetched_bytes=max_fetched_bytes,
    )


async def probe_cloud115_media(
    client: Cloud115Client,
    media_metadata_probe_service: MediaMetadataProbeService,
    *,
    pickcode: str,
    file_size_bytes: int,
) -> tuple[MediaMetadataProbeResult, int]:
    """探测受管目标文件；有效媒体要求返回非空 video_info。"""
    reader = await open_cloud115_range_reader(
        client,
        pickcode=pickcode,
        file_size_bytes=file_size_bytes,
        user_agent=CLOUD115_METADATA_PROBE_UA,
    )
    with reader:
        metadata = media_metadata_probe_service.probe_source(
            reader,
            file_size_bytes=reader.file_size,
            source_label=f"cloud115:{pickcode}",
        )
        fetched_bytes = reader.fetched_bytes
    if metadata.video_info is None:
        raise RuntimeError("video_info_missing_after_probe")
    return metadata, fetched_bytes
