"""115 普通视频导入：云端复制后登记 VideoItem + Media。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

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
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    require_cloud115_library,
)
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.cloud115_import_common import (
    CLOUD115_COVER_UA,
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    Cloud115TargetDirResolver,
    collect_cloud115_source_files,
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
from src.service.transfers.cloud115_media_registrar import Cloud115MediaRegistrar
from src.service.transfers.tag_rules import build_media_special_tags
from src.service.videos.video_collection_service import VideoCollectionService
from src.service.videos.video_cover_service import VideoCoverService

ImportProgressCallback = Callable[[dict[str, object]], None]


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
        only_files: list[str] | None,
    ) -> tuple[str, list[CloudSourceFile]]:
        only_set = set(only_files) if only_files is not None else None
        sources: list[CloudSourceFile] = []
        if source_cid is not None:
            # 安全校验已经查过源目录元信息，直接复用，不再重复 dir_info。
            source_meta = await assert_cid_outside_library_root(
                client, source_cid=source_cid, root_cid=root_cid
            )
            display_path = "/".join(
                [*(crumb.name for crumb in source_meta.paths), source_meta.name]
            )
            # 整树枚举 + 只为视频文件解析父目录名：请求数与目录总数解耦，空目录不会被访问。
            source_entries, rel_dirs = await collect_cloud115_source_files(
                client,
                source_cid,
                needs_rel_path=lambda entry: self._suffix(entry.name)
                in SUPPORTED_VIDEO_EXTENSIONS,
            )
            for entry in source_entries:
                if self._suffix(entry.name) not in SUPPORTED_VIDEO_EXTENSIONS:
                    continue
                rel_dir_parts = rel_dirs[entry.parent_id]
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
        only_files: list[str] | None = None,
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

        failure_items: list[dict] = []
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
        only_files: list[str] | None,
        failure_items: list[dict],
        stats: dict,
        progress_callback: ImportProgressCallback | None,
        job: VideoImportJob,
    ) -> None:
        config = require_cloud115_library(library)
        async with cloud115_client_for(library, batch_pacing=True) as client:
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
            # resolver 按作业缓存 videos/ 段目录 cid，整个作业只解析一次。
            target_dir_resolver = Cloud115TargetDirResolver(
                client, root_cid=config["root_cid"]
            )
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
                existing = Cloud115MediaRegistrar.find_library_media(library, source.sha1)
                # 同批次去重永远跳过（第二个副本没有独立价值）。
                # 库内已存在时的行为按 transfer_mode 分派：
                #   - copy 模式：跳过，保留源文件；
                #   - cleanup-source 模式：先看已登记的 locator.fid 是不是就是这个源。
                #     相同说明上轮登记成功但没搬走，必须补搬运——当成重复副本删掉会
                #     删丢唯一的一份；不同才是真正多余的副本，删源释放 115 配额。
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
                    seen_sha1.add(source.sha1)
                    registered_fid = str(
                        (existing.backend_locator or {}).get("fid") or ""
                    )
                    if registered_fid == source.fid:
                        # 同一个远端文件：上轮登记成功但没搬走，补完搬运即可。
                        await self._move_registered_source(
                            client,
                            media=existing,
                            source=source,
                            target_dir_resolver=target_dir_resolver,
                            failure_items=failure_items,
                            stats=stats,
                        )
                        continue
                    stats["skipped"] += 1
                    failure_items.append(
                        make_failure_item(
                            source.rel_path,
                            FAILURE_REASON_DUPLICATE_FINGERPRINT,
                            f"库中已存在相同内容（media_id={existing.id}）",
                        )
                    )
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
                    target_dir_resolver=target_dir_resolver,
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
        target_dir_resolver: Cloud115TargetDirResolver,
        transfer_mode: str,
        collection_id: int | None,
        failure_items: list[dict],
        stats: dict,
    ) -> None:
        """把一个 115 源视频搬进受管结构 ``videos/{video_id}/{版本ms}/{原名}``。

        共用前半段：probe(**源 pickcode**，跳过"必须先落到受管目录才能读"的鸡生蛋)
        → create VideoItem 拿 id → 建目标目录。之后按模式分岔：

        - ``copy``：复制 → re-list 对账拿新 fid/pickcode → rename → 登记 Media。
        - ``cleanup-source``：**先登记 Media**（locator 用源 fid/pickcode，move 不改这
          两个值，Media 也只靠 pickcode 定位）→ move。搬运失败时 Media 依然可播、
          源也还在原处，下轮重导按 locator.fid 认出来补搬运即收敛，不留无记录的孤儿。
        """
        moving = transfer_mode == CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
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

        # 3) 建 videos/{video_id}/{版本ms}/。video.id 刚从数据库自增拿到，实体目录
        # 必然不存在，直接两次 mkdir，不枚举 videos/ 下的既有实体目录。
        try:
            entity_cid, version_cid = (
                await target_dir_resolver.create_new_videos_entity_dirs(
                    video_id=video.id,
                    now_ms=int(time.time() * 1000),
                )
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

        # 4) copy 模式先复制并 re-list 对账拿新定位；move 模式的搬运推迟到登记之后，
        # 定位直接沿用源 fid/pickcode。
        locator_fid, locator_pickcode = source.fid, source.pickcode
        if not moving:
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
                    target_fid=(
                        target_entry.entry_id if target_entry is not None else None
                    ),
                )
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        source.rel_path,
                        FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                        str(exc),
                    )
                )
                return
            locator_fid, locator_pickcode = (
                target_entry.entry_id,
                target_entry.pickcode,
            )

        # 5) copy 出来的默认文件名通常等于源名，一般不需要 rename；名字不一致时对齐目标名。
        # move 保持文件名不变，videos 又沿用原名，因此移动模式下这一步天然不需要。
        if target_entry is not None and target_entry.name != target_name:
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
        locator = Cloud115MediaRegistrar.build_locator(
            fid=locator_fid,
            pickcode=locator_pickcode,
            name=target_name,
            source_path=source.rel_path,
        )
        try:
            with get_database().atomic():
                media = Cloud115MediaRegistrar.create_cloud115_media(
                    video_item=video,
                    library=library,
                    locator=locator,
                    fingerprint=Cloud115MediaRegistrar.build_fingerprint(source.sha1),
                    file_size_bytes=source.size,
                    storage_mode="move" if moving else "copy",
                    resolution=metadata.resolution if metadata is not None else None,
                    duration_seconds=duration_seconds,
                    video_info=effective_video_info,
                    special_tags=special_tags,
                    valid=not source.censored,
                )
                if collection_id is not None:
                    VideoCollectionService.add_item(collection_id, video.id)
        except Exception as exc:
            # 移动模式此时还没搬运，远端只有本轮新建的空目录，可以整体回滚干净。
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
                    source.rel_path, FAILURE_REASON_MEDIA_IMPORT_FAILED, str(exc)
                )
            )
            return

        # 6) 封面放在搬运之前：pickcode 与文件所在目录无关，移动前后都能读，
        # 这样移动失败后的补搬运重试不必再生成一次封面。
        if not source.censored:
            Cloud115MediaRegistrar.reset_thumbnail_state(media.id)
            try:
                reader = await open_cloud115_range_reader(
                    client,
                    pickcode=locator_pickcode,
                    file_size_bytes=source.size,
                    user_agent=CLOUD115_COVER_UA,
                )
                with reader:
                    VideoCoverService.generate_cover(video, reader)
            except Exception as exc:
                logger.warning(
                    "Cloud115 video cover skipped video_id={} detail={}", video.id, exc
                )

        # 7) 移动模式在登记之后搬运：失败时 Media 仍可播、源也还在原处，
        # 失败项可重导，下轮按 locator.fid 认出来补搬运即收敛。
        if moving:
            try:
                await client.move_files([source.fid], pid=version_cid)
            except Exception as exc:
                await self._discard_empty_version_dir(
                    client, version_cid, video_id=video.id
                )
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        source.rel_path,
                        FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                        str(exc),
                    )
                )
                logger.warning(
                    "Cloud115 video move failed video_id={} rel_path={} detail={}",
                    video.id, source.rel_path, exc,
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

    async def _move_registered_source(
        self,
        client: Cloud115Client,
        *,
        media: Media,
        source: CloudSourceFile,
        target_dir_resolver: Cloud115TargetDirResolver,
        failure_items: list[dict],
        stats: dict,
    ) -> None:
        """已登记但上轮没搬走的源：补建目录并移动即可。

        locator 不需要变更——move 不改 fid / pickcode，文件名也保持原样，登记时写入的
        定位信息在搬运前后完全一致。实体目录上轮可能已经建过，所以走 find-or-create。
        """
        if media.video_item_id is None:
            # find_library_media 只按 library + sha1 查、不分域，命中的可能是同库的 JAV
            # Media（video_item 为空）。它的 locator 指向的就是这个源文件本身，既不能按
            # videos 布局搬走（video_id 为空会建出 videos/None/），也不能删源。
            stats["skipped"] += 1
            failure_items.append(
                make_failure_item(
                    source.rel_path,
                    FAILURE_REASON_DUPLICATE_FINGERPRINT,
                    f"该文件已被 JAV 影片记录占用（media_id={media.id}）",
                )
            )
            logger.warning(
                "Cloud115 video re-move skipped non-video media media_id={} rel_path={}",
                media.id, source.rel_path,
            )
            return
        try:
            version_cid = await target_dir_resolver.prepare_videos_version_dir(
                video_id=media.video_item_id,
                now_ms=int(time.time() * 1000),
            )
            await client.move_files([source.fid], pid=version_cid)
        except Exception as exc:
            stats["failed"] += 1
            failure_items.append(
                make_failure_item(
                    source.rel_path,
                    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    f"补搬运已登记文件失败: {exc}",
                )
            )
            logger.warning(
                "Cloud115 video re-move failed media_id={} rel_path={} detail={}",
                media.id, source.rel_path, exc,
            )
            return
        stats["imported"] += 1
        logger.info(
            "Cloud115 video re-moved registered source media_id={} rel_path={}",
            media.id, source.rel_path,
        )

    @staticmethod
    async def _discard_empty_version_dir(
        client: Cloud115Client, version_cid: str, *, video_id: int
    ) -> None:
        """回收本轮新建、最终没搬进文件的空版本目录；失败仅告警。"""
        try:
            await client.delete_files([version_cid])
        except Exception as exc:
            logger.warning(
                "Cloud115 video version dir discard failed video_id={} cid={} detail={}",
                video_id, version_cid, exc,
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
        failure_items: list[dict],
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
