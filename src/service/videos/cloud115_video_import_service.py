"""115 普通视频导入：云端复制后登记 VideoItem + Media。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable, Dict, List

from loguru import logger

from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.media_import_status import (
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
    FAILURE_REASON_CLOUD115_RENAME_FAILED,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_IMPORT_JOB_CRASHED,
    FAILURE_REASON_MEDIA_IMPORT_FAILED,
    FAILURE_REASON_RETRY_SOURCES_MISSING,
    FAILURE_REASON_SOURCE_DELETE_FAILED,
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    make_failure_item,
)
from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import Cloud115Client, DirEntry
from src.model import Media, MediaLibrary, VideoImportJob, VideoItem, get_database
from src.service.playback.cloud115_backend_service import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    require_cloud115_library,
)
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.system.resource_task_state_service import ResourceTaskStateService
from src.service.transfers.cloud115_import_common import (
    CLOUD115_COVER_UA,
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    build_cloud115_dir_map,
    cloud115_rel_dir_parts,
    ensure_cloud115_videos_target_dir,
    list_cloud115_target_files,
    normalize_cloud115_transfer_mode,
    open_cloud115_range_reader,
    probe_cloud115_media,
    resolve_cloud115_copied_entry,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115_import_service import (
    CloudSourceFile,
)
from src.service.transfers.tag_rules import build_media_special_tags
from src.service.videos.video_collection_service import VideoCollectionService
from src.service.videos.video_cover_service import VideoCoverService

ImportProgressCallback = Callable[[Dict[str, object]], None]


class Cloud115VideoImportService:
    def __init__(
        self,
        media_metadata_probe_service: MediaMetadataProbeService | None = None,
    ) -> None:
        self._media_metadata_probe_service = (
            media_metadata_probe_service or MediaMetadataProbeService()
        )

    @staticmethod
    def _emit(callback: ImportProgressCallback | None, **payload: object) -> None:
        if callback is not None:
            callback(payload)

    @staticmethod
    def _suffix(name: str) -> str:
        return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""

    @staticmethod
    def _find_library_media(library: MediaLibrary, sha1: str) -> Media | None:
        return (
            Media.select()
            .where(
                Media.library == library,
                Media.content_fingerprint == f"sha1:{sha1}",
            )
            .order_by(Media.id.desc())
            .first()
        )

    @staticmethod
    async def _assert_file_outside_library_root(
        client: Cloud115Client,
        *,
        parent_cid: str,
        root_cid: str,
    ) -> None:
        if parent_cid == root_cid:
            raise ValueError("cloud115_source_inside_library")
        parent_meta = await client.dir_info(parent_cid)
        if any(crumb.file_id == root_cid for crumb in parent_meta.paths):
            raise ValueError("cloud115_source_inside_library")

    @staticmethod
    async def _entry_by_fid(
        client: Cloud115Client,
        *,
        parent_cid: str,
        fid: str,
    ) -> DirEntry | None:
        offset = 0
        while True:
            entries, total = await client.list_dir(parent_cid, offset=offset, limit=1150)
            for entry in entries:
                if not entry.is_dir and entry.entry_id == fid:
                    return entry
            offset += len(entries)
            if not entries or offset >= total:
                return None

    async def _collect_sources(
        self,
        client: Cloud115Client,
        *,
        source_cid: str | None,
        source_fid: str | None,
        root_cid: str,
        only_files: List[str] | None,
    ) -> tuple[str, List[CloudSourceFile]]:
        only_set = set(only_files) if only_files is not None else None
        sources: List[CloudSourceFile] = []
        if source_cid is not None:
            await assert_cid_outside_library_root(
                client, source_cid=source_cid, root_cid=root_cid
            )
            source_meta = await client.dir_info(source_cid)
            display_path = "/".join(
                [*(crumb.name for crumb in source_meta.paths), source_meta.name]
            )
            dir_map = await build_cloud115_dir_map(client, source_cid)
            async for entry in client.iter_files_recursive(source_cid):
                if self._suffix(entry.name) not in SUPPORTED_VIDEO_EXTENSIONS:
                    continue
                rel_dir_parts = cloud115_rel_dir_parts(
                    entry.parent_id, dir_map, source_cid
                )
                rel_path = "/".join([*rel_dir_parts, entry.name])
                if only_set is not None and rel_path not in only_set:
                    continue
                if not entry.sha1:
                    sources.append(
                        CloudSourceFile(
                            fid=entry.entry_id,
                            pickcode=entry.pickcode,
                            name=entry.name,
                            sha1="",
                            size=entry.size,
                            play_long=entry.play_long,
                            censored=entry.ic == 1,
                            rel_dir_parts=rel_dir_parts,
                            parent_cid=entry.parent_id,
                        )
                    )
                    continue
                sources.append(
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
            return display_path or f"cloud115:{source_cid}", sorted(
                sources, key=lambda item: item.rel_path
            )

        if source_fid is None:
            raise ValueError("cloud115_video_source_required")
        file_meta = await client.file_info(source_fid)
        await self._assert_file_outside_library_root(
            client, parent_cid=file_meta.parent_id, root_cid=root_cid
        )
        if self._suffix(file_meta.name) not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError("import_source_unsupported")
        parent_meta = await client.dir_info(file_meta.parent_id)
        display_path = "/".join(
            [
                *(crumb.name for crumb in parent_meta.paths),
                parent_meta.name,
                file_meta.name,
            ]
        )
        entry = await self._entry_by_fid(
            client, parent_cid=file_meta.parent_id, fid=source_fid
        )
        rel_path = file_meta.name
        if only_set is not None and rel_path not in only_set:
            return display_path, []
        sources.append(
            CloudSourceFile(
                fid=file_meta.file_id,
                pickcode=file_meta.pickcode,
                name=file_meta.name,
                sha1=file_meta.sha1.upper() if file_meta.sha1 else "",
                size=file_meta.size,
                play_long=entry.play_long if entry is not None else None,
                censored=entry.ic == 1 if entry is not None else False,
                rel_dir_parts=(),
                parent_cid=file_meta.parent_id,
            )
        )
        return display_path, sources

    @staticmethod
    def _prepare_job(
        *,
        video_import_job_id: int | None,
        library: MediaLibrary,
        source_cid: str | None,
        source_fid: str | None,
        transfer_mode: str,
        collection_id: int | None,
    ) -> VideoImportJob:
        placeholder = f"cloud115:{source_cid or source_fid}"
        if video_import_job_id is None:
            return VideoImportJob.create(
                source_path=placeholder,
                source_cid=source_cid,
                source_fid=source_fid,
                library=library,
                collection=collection_id,
                state=IMPORT_JOB_STATE_PENDING,
                transfer_mode=transfer_mode,
            )
        job = VideoImportJob.get_by_id(video_import_job_id)
        job.source_path = placeholder
        job.source_cid = source_cid
        job.source_fid = source_fid
        job.library = library
        job.collection = collection_id
        job.transfer_mode = transfer_mode
        job.state = IMPORT_JOB_STATE_PENDING
        job.imported_count = 0
        job.skipped_count = 0
        job.failed_count = 0
        job.failed_files = "[]"
        job.started_at = None
        job.finished_at = None
        job.save()
        return job

    def import_from_cloud115(
        self,
        library_id: int,
        *,
        source_cid: str | None = None,
        source_fid: str | None = None,
        video_import_job_id: int | None = None,
        transfer_mode: str = "copy",
        collection_id: int | None = None,
        only_files: List[str] | None = None,
        progress_callback: ImportProgressCallback | None = None,
    ) -> VideoImportJob:
        transfer_mode = normalize_cloud115_transfer_mode(
            transfer_mode, allow_legacy_move=False
        )
        if (source_cid is None) == (source_fid is None):
            raise ValueError("exactly_one_cloud115_video_source_required")
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ValueError("media_library_not_found")
        require_cloud115_library(library)
        if collection_id is not None:
            VideoCollectionService._require_collection(collection_id)

        job = self._prepare_job(
            video_import_job_id=video_import_job_id,
            library=library,
            source_cid=source_cid,
            source_fid=source_fid,
            transfer_mode=transfer_mode,
            collection_id=collection_id,
        )
        job.state = IMPORT_JOB_STATE_RUNNING
        job.started_at = utc_now_for_db()
        job.save()

        failure_items: List[dict] = []
        stats = {"imported": 0, "skipped": 0, "failed": 0}
        try:
            asyncio.run(
                self._run(
                    library=library,
                    source_cid=source_cid,
                    source_fid=source_fid,
                    transfer_mode=transfer_mode,
                    collection_id=collection_id,
                    only_files=only_files,
                    failure_items=failure_items,
                    stats=stats,
                    progress_callback=progress_callback,
                    job=job,
                )
            )
        except Exception as exc:
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(job.source_path, FAILURE_REASON_IMPORT_JOB_CRASHED, str(exc))
            )
            logger.exception("Cloud115 video import crashed job_id={}", job.id)

        job.imported_count = stats["imported"]
        job.skipped_count = stats["skipped"]
        job.failed_count = stats["failed"]
        job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        job.state = IMPORT_JOB_STATE_FAILED if stats["failed"] else IMPORT_JOB_STATE_COMPLETED
        job.finished_at = utc_now_for_db()
        job.save()
        return job

    async def _run(
        self,
        *,
        library: MediaLibrary,
        source_cid: str | None,
        source_fid: str | None,
        transfer_mode: str,
        collection_id: int | None,
        only_files: List[str] | None,
        failure_items: List[dict],
        stats: dict,
        progress_callback: ImportProgressCallback | None,
        job: VideoImportJob,
    ) -> None:
        config = require_cloud115_library(library)
        async with cloud115_client_for(library) as client:
            display_path, sources = await self._collect_sources(
                client,
                source_cid=source_cid,
                source_fid=source_fid,
                root_cid=config["root_cid"],
                only_files=only_files,
            )
            job.source_path = display_path[:1024]
            job.save()
            if only_files is not None and not sources:
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        job.source_path,
                        FAILURE_REASON_RETRY_SOURCES_MISSING,
                        "待重导的源文件均已不存在",
                    )
                )
                return

            # 每个视频独立版本目录（videos/{video_id}/{版本ms}/），跨视频不共享。
            root_cid = config["root_cid"]
            seen_sha1: set[str] = set()
            total = len(sources)
            self._emit(
                progress_callback,
                event="scan_complete",
                current=0,
                total=total,
                text="115 视频扫描完成",
            )

            for index, source in enumerate(sources, start=1):
                self._emit(
                    progress_callback,
                    event="file_started",
                    current=index - 1,
                    total=total,
                    text=f"正在导入 {source.rel_path}",
                )
                if not source.sha1:
                    stats["failed"] += 1
                    failure_items.append(
                        make_failure_item(
                            source.rel_path,
                            FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                            "115 未返回文件 sha1，无法对账",
                        )
                    )
                    continue
                existing = self._find_library_media(library, source.sha1)
                # 同批次去重永远跳过（第二个副本没有独立价值）。
                # 库内已存在时的行为按 transfer_mode 分派：
                #   - copy 模式：跳过，保留源文件；
                #   - cleanup-source 模式：不再重登，但**必须**尝试删除 115 源文件——
                #     否则一次 delete_files 瞬时失败会让源永久滞留 115，用户看不到
                #     可操作的失败项，违背 cleanup-source"复制后释放 115 配额"的语义。
                if source.sha1 in seen_sha1:
                    stats["skipped"] += 1
                    failure_items.append(
                        make_failure_item(
                            source.rel_path,
                            FAILURE_REASON_DUPLICATE_FINGERPRINT,
                            "同批次存在相同内容文件",
                        )
                    )
                    continue
                if existing is not None and transfer_mode != CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE:
                    stats["skipped"] += 1
                    failure_items.append(
                        make_failure_item(
                            source.rel_path,
                            FAILURE_REASON_DUPLICATE_FINGERPRINT,
                            f"库中已存在相同内容（media_id={existing.id}）",
                        )
                    )
                    continue
                if existing is not None:
                    stats["skipped"] += 1
                    failure_items.append(
                        make_failure_item(
                            source.rel_path,
                            FAILURE_REASON_DUPLICATE_FINGERPRINT,
                            f"库中已存在相同内容（media_id={existing.id}）",
                        )
                    )
                    seen_sha1.add(source.sha1)
                    await self._cleanup_source_only(
                        client,
                        source=source,
                        failure_items=failure_items,
                        stats=stats,
                    )
                    continue
                seen_sha1.add(source.sha1)
                await self._import_file(
                    client,
                    library=library,
                    source=source,
                    root_cid=root_cid,
                    transfer_mode=transfer_mode,
                    collection_id=collection_id,
                    failure_items=failure_items,
                    stats=stats,
                )
                self._emit(
                    progress_callback,
                    event="file_finished",
                    current=index,
                    total=total,
                    text=f"已处理 {source.rel_path}",
                )

    async def _import_file(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        source: CloudSourceFile,
        root_cid: str,
        transfer_mode: str,
        collection_id: int | None,
        failure_items: List[dict],
        stats: dict,
    ) -> None:
        """把一个 115 源视频复制进受管结构 ``videos/{video_id}/{版本ms}/{原名}``。

        对齐本地 VideoImportService 的顺序：先 probe → create VideoItem 拿 id →
        建目标目录 → copy → rename → create Media。probe 使用 **源 pickcode**，
        跳过"必须先落到受管目录才能读"的鸡生蛋。
        """
        target_name = source.name  # videos 域对齐本地：保留原文件名，不做 rename
        # 1) 用源 pickcode probe，因为目标目录还没建。
        metadata: MediaMetadataProbeResult | None = None
        if not source.censored:
            try:
                metadata, fetched_bytes = await probe_cloud115_media(
                    client,
                    self._media_metadata_probe_service,
                    pickcode=source.pickcode,
                    file_size_bytes=source.size,
                )
                logger.info(
                    "Cloud115 video metadata probed rel_path={} fetched_bytes={}",
                    source.rel_path,
                    fetched_bytes,
                )
            except Exception as exc:
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        source.rel_path,
                        FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
                        str(exc),
                    )
                )
                return

        # 2) create VideoItem 拿到 video_id，作为实体目录名。
        video = VideoItem.create(
            title=Path(source.name).stem,
            release_date=metadata.creation_time if metadata is not None else None,
        )

        # entity_cid / version_cid / target_fid 每推进一步就落一个变量，
        # 任一失败分支统一走 _rollback_video_import 反向清理，避免 VideoItem
        # 与已 copy 的 115 文件成为孤儿（对齐本地 video_import_service 的语义）。
        entity_cid: str | None = None
        version_cid: str | None = None
        target_entry: DirEntry | None = None

        # 3) 建 videos/{video_id}/{版本ms}/。
        try:
            entity_cid, version_cid = await ensure_cloud115_videos_target_dir(
                client,
                root_cid=root_cid,
                video_id=video.id,
                now_ms=int(time.time() * 1000),
            )
        except Exception as exc:
            await self._rollback_video_import(
                client,
                video=video,
                entity_cid=entity_cid,
                version_cid=version_cid,
                target_fid=None,
            )
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(
                    source.rel_path,
                    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    f"创建视频目录失败: {exc}",
                )
            )
            return

        # 4) copy → list → 拿到目标 entry。
        try:
            await client.copy_files([source.fid], pid=version_cid)
            version_entries = await list_cloud115_target_files(client, version_cid)
            target_entry = resolve_cloud115_copied_entry(
                version_entries, source, target_name
            )
            if target_entry is None:
                raise RuntimeError(f"复制后未找到 sha1={source.sha1} 的目标文件")
        except Exception as exc:
            await self._rollback_video_import(
                client,
                video=video,
                entity_cid=entity_cid,
                version_cid=version_cid,
                target_fid=target_entry.entry_id if target_entry is not None else None,
            )
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(
                    source.rel_path, FAILURE_REASON_CLOUD115_TRANSFER_FAILED, str(exc)
                )
            )
            return

        # 5) copy 出来的默认文件名通常等于源名，一般不需要 rename；名字不一致时对齐目标名。
        if target_entry.name != target_name:
            try:
                await client.rename_file(target_entry.entry_id, target_name)
                await verify_cloud115_renamed_file(
                    client, target_entry.entry_id, target_name
                )
            except Exception as exc:
                await self._rollback_video_import(
                    client,
                    video=video,
                    entity_cid=entity_cid,
                    version_cid=version_cid,
                    target_fid=target_entry.entry_id,
                )
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        source.rel_path, FAILURE_REASON_CLOUD115_RENAME_FAILED, str(exc)
                    )
                )
                return

        effective_video_info = metadata.video_info if metadata is not None else None
        special_tags = build_media_special_tags(
            [source.rel_path], "", video_info=effective_video_info, has_subtitle=False
        )
        duration_seconds = (
            (metadata.duration_seconds or source.play_long or 0)
            if metadata is not None
            else (source.play_long or 0)
        )
        locator = {
            "fid": target_entry.entry_id,
            "pickcode": target_entry.pickcode,
            "name": target_name,
            "source_path": source.rel_path,
        }
        try:
            with get_database().atomic():
                media = Media.create(
                    video_item=video,
                    library=library,
                    backend_locator=locator,
                    storage_mode="copy",
                    content_fingerprint=f"sha1:{source.sha1}",
                    file_size_bytes=source.size,
                    resolution=metadata.resolution if metadata is not None else None,
                    duration_seconds=duration_seconds,
                    video_info=effective_video_info,
                    special_tags=special_tags,
                    valid=not source.censored,
                )
                if collection_id is not None:
                    VideoCollectionService.add_item(collection_id, video.id)
        except Exception as exc:
            await self._rollback_video_import(
                client,
                video=video,
                entity_cid=entity_cid,
                version_cid=version_cid,
                target_fid=target_entry.entry_id,
            )
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(
                    source.rel_path, FAILURE_REASON_MEDIA_IMPORT_FAILED, str(exc)
                )
            )
            return

        stats["imported"] += 1
        if source.censored:
            failure_items.append(
                make_failure_item(
                    source.rel_path,
                    FAILURE_REASON_CLOUD115_FILE_CENSORED,
                    "115 标记该文件违规（ic=1）",
                )
            )
        else:
            ResourceTaskStateService.reset_for_requeue(
                MediaThumbnailService.TASK_KEY, media.id
            )
            try:
                reader = await open_cloud115_range_reader(
                    client,
                    pickcode=target_entry.pickcode,
                    file_size_bytes=source.size,
                    user_agent=CLOUD115_COVER_UA,
                )
                with reader:
                    VideoCoverService.generate_cover(video, reader)
            except Exception as exc:
                logger.warning(
                    "Cloud115 video cover skipped video_id={} detail={}", video.id, exc
                )

        if transfer_mode == CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE:
            try:
                await client.delete_files([source.fid])
            except Exception as exc:
                stats["failed"] += 1
                item = make_failure_item(
                    source.rel_path, FAILURE_REASON_SOURCE_DELETE_FAILED, str(exc)
                )
                # 清源失败必须走可操作路径：与 JAV 管线对齐把 kind 提升为 file，
                # 让用户能通过失败项重试列表精确地重新触发删除，避免残留 115 侧文件。
                item["kind"] = "file"
                failure_items.append(item)
                logger.warning(
                    "Cloud115 video source delete failed rel_path={} detail={}",
                    source.rel_path, exc,
                )

    @staticmethod
    async def _rollback_video_import(
        client: Cloud115Client,
        *,
        video: VideoItem,
        entity_cid: str | None,
        version_cid: str | None,
        target_fid: str | None,
    ) -> None:
        """按已推进的步骤反向清理云端 + VideoItem。每步失败仅告警不中断，尽力回滚。

        顺序：文件（target_fid）→ 版本目录 → 实体目录 → VideoItem。
        videos 侧的 entity_cid（``videos/{video_id}/``）是本次导入独占，可以放心删。
        """
        if target_fid is not None:
            try:
                await client.delete_files([target_fid])
            except Exception as exc:
                logger.warning(
                    "Cloud115 video rollback file failed video_id={} fid={} detail={}",
                    video.id, target_fid, exc,
                )
        if version_cid is not None:
            try:
                await client.delete_files([version_cid])
            except Exception as exc:
                logger.warning(
                    "Cloud115 video rollback version_cid failed video_id={} cid={} detail={}",
                    video.id, version_cid, exc,
                )
        if entity_cid is not None:
            try:
                await client.delete_files([entity_cid])
            except Exception as exc:
                logger.warning(
                    "Cloud115 video rollback entity_cid failed video_id={} cid={} detail={}",
                    video.id, entity_cid, exc,
                )
        try:
            video.delete_instance()
        except Exception as exc:
            logger.warning(
                "Cloud115 video rollback VideoItem delete failed video_id={} detail={}",
                video.id, exc,
            )

    async def _cleanup_source_only(
        self,
        client: Cloud115Client,
        *,
        source: CloudSourceFile,
        failure_items: List[dict],
        stats: dict,
    ) -> None:
        """cleanup-source 模式下遇到已入库内容时，只删远端源文件、不重登。

        与 _import_file 的清源分支共享失败语义（kind=file 可重试），保证 delete
        瞬时失败时用户能从失败项列表精确重跑，不至于永久遗留 115 侧文件。
        """
        try:
            await client.delete_files([source.fid])
        except Exception as exc:
            stats["failed"] += 1
            item = make_failure_item(
                source.rel_path, FAILURE_REASON_SOURCE_DELETE_FAILED, str(exc)
            )
            item["kind"] = "file"
            failure_items.append(item)
            logger.warning(
                "Cloud115 video cleanup-only delete failed rel_path={} detail={}",
                source.rel_path, exc,
            )
