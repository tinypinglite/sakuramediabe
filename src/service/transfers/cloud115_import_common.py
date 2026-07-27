"""cloud115 导入公共原语：模式、目录解析、受预算媒体探测与 RangeReader 构造。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from src.lib.cloud115 import Cloud115Client, Cloud115RangeReader, DirEntry
from src.service.cloud115 import find_or_create_subdir
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.file_transfer import (
    JAV_LIBRARY_SUBDIR,
    VIDEOS_LIBRARY_SUBDIR,
)

CLOUD115_TRANSFER_MODE_COPY = "copy"
CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE = "cleanup-source"
CLOUD115_TRANSFER_MODE_LEGACY_MOVE = "move"

CLOUD115_METADATA_PROBE_MAX_BYTES = 64 * 1024 * 1024
CLOUD115_METADATA_PROBE_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Metadata/1.0"
CLOUD115_COVER_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Cover/1.0"


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


# ---------------------------------------------------------------------------
# 分层目录构造：把 115 的存储结构对齐本地 MediaImportService/VideoImportService。
# 布局：
#   <root_cid>/jav/<番号>/<版本ms>/<番号>{ext}
#   <root_cid>/videos/<video_id>/<版本ms>/<原文件名>
# ---------------------------------------------------------------------------


def normalize_jav_media_filename(movie_number: str, source_name: str) -> str:
    """按本地 JAV 命名规范：``{番号}{源扩展名小写}``。

    源无扩展名时（例如 ``.iso`` 之外的裸文件）沿用本地 ``_import_single_media_file``
    行为返回不带扩展名的番号。
    """
    if not movie_number:
        raise ValueError("movie_number is required for JAV rename")
    ext = Path(source_name).suffix.lower()
    return f"{movie_number}{ext}"


async def create_cloud115_version_subdir(
    client: Cloud115Client, *, parent_cid: str, now_ms: int
) -> str:
    """在 parent_cid 下建一个新版本目录，冲突时按 ``{ms}-N`` 递增。

    对齐 ``file_transfer.create_version_directory``：先分页 list 判存在再建，
    因为 115 允许同名目录并存。
    """
    base = str(now_ms)
    candidate = base
    suffix = 1
    while True:
        existing = await _lookup_cloud115_subdir_cid(
            client, parent_cid=parent_cid, name=candidate
        )
        if existing is None:
            return await client.mkdir(parent_cid, candidate)
        candidate = f"{base}-{suffix}"
        suffix += 1


async def _lookup_cloud115_subdir_cid(
    client: Cloud115Client, *, parent_cid: str, name: str
) -> str | None:
    offset = 0
    while True:
        entries, total = await client.list_dir(parent_cid, offset=offset, limit=1150)
        for entry in entries:
            if entry.is_dir and entry.name == name:
                return entry.entry_id
        offset += len(entries)
        if not entries or offset >= total:
            return None


async def ensure_cloud115_jav_target_dir(
    client: Cloud115Client,
    *,
    root_cid: str,
    movie_number: str,
    now_ms: int,
) -> tuple[str, str]:
    """建 ``jav/{番号}/{版本ms}/``，返回 ``(entity_cid, version_cid)``。"""
    jav_cid = await find_or_create_subdir(client, parent_cid=root_cid, name=JAV_LIBRARY_SUBDIR)
    entity_cid = await find_or_create_subdir(client, parent_cid=jav_cid, name=movie_number)
    version_cid = await create_cloud115_version_subdir(
        client, parent_cid=entity_cid, now_ms=now_ms
    )
    return entity_cid, version_cid


async def ensure_cloud115_videos_target_dir(
    client: Cloud115Client,
    *,
    root_cid: str,
    video_id: int,
    now_ms: int,
) -> tuple[str, str]:
    """建 ``videos/{video_id}/{版本ms}/``，返回 ``(entity_cid, version_cid)``。"""
    videos_cid = await find_or_create_subdir(
        client, parent_cid=root_cid, name=VIDEOS_LIBRARY_SUBDIR
    )
    entity_cid = await find_or_create_subdir(
        client, parent_cid=videos_cid, name=str(video_id)
    )
    version_cid = await create_cloud115_version_subdir(
        client, parent_cid=entity_cid, now_ms=now_ms
    )
    return entity_cid, version_cid


async def _list_subdir_cids_by_name(
    client: Cloud115Client, parent_cid: str
) -> Dict[str, str]:
    """列 parent_cid 下所有子目录，返回 ``{目录名: cid}``。

    同名目录只保留首个命中，语义对齐 ``find_or_create_subdir``（命中即返回第一个）。
    """
    result: Dict[str, str] = {}
    offset = 0
    while True:
        entries, total = await client.list_dir(parent_cid, offset=offset, limit=1150)
        for entry in entries:
            if entry.is_dir and entry.name not in result:
                result[entry.name] = entry.entry_id
        offset += len(entries)
        if not entries or offset >= total:
            break
    return result


@dataclass
class Cloud115TargetDirCache:
    """自动导入批次或单次手动作业内共享的目标目录缓存。"""

    section_cids: Dict[str, str] = field(default_factory=dict)
    entities: Dict[str, Dict[str, str]] = field(default_factory=dict)


class Cloud115TargetDirResolver:
    """批次级目录缓存：把"每条 item 翻整个 jav//videos/ 找实体目录"降为"整批只翻一次"。

    115 webapi 的 ``GET /files``（列目录）是批量秒传触发风控的主要请求来源：原本每条
    item 都要列 root 找 jav、列整个 jav/ 找番号、再列番号目录防版本重名。本解析器：
      - jav/videos 段目录 cid 解析一次即缓存；
      - 首次访问某段时翻一次该段目录，建 ``{实体名: cid}`` 表，之后只查表；
      - 批次内新建的实体目录写回缓存；
      - 版本目录名是毫秒时间戳，天然唯一，直接 mkdir，不再预列表去重。
    结果：每条 item 的 ``GET /files`` 从约 3 次降到 0 次（整批仅段解析 + 首次建表各一次）。

    缓存只在同一自动导入批次内共享；批次内新番号由本解析器自建并登记。
    """

    def __init__(
        self,
        client: Cloud115Client,
        *,
        root_cid: str,
        cache: Cloud115TargetDirCache | None = None,
    ) -> None:
        self._client = client
        self._root_cid = root_cid
        self._cache = cache or Cloud115TargetDirCache()

    async def _section_cid_for(self, section: str) -> str:
        cid = self._cache.section_cids.get(section)
        if cid is None:
            cid = await find_or_create_subdir(
                self._client, parent_cid=self._root_cid, name=section
            )
            self._cache.section_cids[section] = cid
        return cid

    async def _entity_map_for(self, section: str) -> Dict[str, str]:
        entities = self._cache.entities.get(section)
        if entities is None:
            section_cid = await self._section_cid_for(section)
            entities = await _list_subdir_cids_by_name(self._client, section_cid)
            self._cache.entities[section] = entities
        return entities

    async def _resolve_entity_cid(
        self,
        section: str,
        entity_name: str,
    ) -> tuple[str, bool]:
        entities = await self._entity_map_for(section)
        cid = entities.get(entity_name)
        created = cid is None
        if cid is None:
            section_cid = await self._section_cid_for(section)
            cid = await self._client.mkdir(section_cid, entity_name)
            entities[entity_name] = cid
        return cid, created

    async def resolve_jav_entity(self, movie_number: str) -> tuple[str, bool]:
        """解析番号目录，并返回本批次是否刚创建。"""
        return await self._resolve_entity_cid(JAV_LIBRARY_SUBDIR, movie_number)

    async def create_version_dir(self, *, entity_cid: str, now_ms: int) -> str:
        """版本目录使用毫秒时间戳命名，直接创建，不做预列表。"""
        return await self._client.mkdir(entity_cid, str(now_ms))

    async def prepare_jav_version_dir(self, *, movie_number: str, now_ms: int) -> str:
        """建 ``jav/{番号}/{版本ms}/``，返回版本目录 cid。"""
        entity_cid, _ = await self.resolve_jav_entity(movie_number)
        return await self.create_version_dir(entity_cid=entity_cid, now_ms=now_ms)

    async def prepare_videos_version_dir(self, *, video_id: int, now_ms: int) -> str:
        """建 ``videos/{video_id}/{版本ms}/``，返回版本目录 cid。"""
        entity_cid, _ = await self._resolve_entity_cid(
            VIDEOS_LIBRARY_SUBDIR,
            str(video_id),
        )
        return await self.create_version_dir(entity_cid=entity_cid, now_ms=now_ms)


async def list_cloud115_entity_target_files(
    client: Cloud115Client, entity_cid: str
) -> Dict[str, List[DirEntry]]:
    """递归列实体目录（jav/番号 或 videos/id）下所有版本目录里的文件，按 SHA1 索引。

    用于按番号/视频粒度做中断恢复对账：一个实体的多次导入分散在多个版本子目录里，
    需要跨版本聚合。
    """
    by_sha1: Dict[str, List[DirEntry]] = {}
    async for entry in client.iter_files_recursive(entity_cid):
        if not entry.is_dir and entry.sha1:
            by_sha1.setdefault(entry.sha1.upper(), []).append(entry)
    return by_sha1


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


async def collect_cloud115_source_tree(
    client: Cloud115Client,
    source_cid: str,
) -> tuple[Dict[str, tuple[str, str]], List[DirEntry]]:
    """一次 BFS 同时收集目录映射与文件，避免随后再次递归枚举同一来源。"""
    dir_map: Dict[str, tuple[str, str]] = {}
    files: List[DirEntry] = []
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
                else:
                    files.append(entry)
            offset += len(entries)
            if not entries or offset >= total:
                break
    return dir_map, files


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
