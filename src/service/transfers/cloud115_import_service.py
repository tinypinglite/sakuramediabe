"""cloud115 复制式导入管线（JAV）。

与本地 ``MediaImportService.import_from_source`` 对称的云端版本：用户指定 115 源目录
（cid），管线把其中的 JAV 视频复制进库管理目录 ``sakuramedia/jav/``（copy 或
cleanup-source），字幕 ``.srt`` 下载到本地 ``subtitle_root/{番号}/``，最后登记 Media（path 为空、
backend_locator 定位）。

与本地管线的关键差异（依据 docs/development/cloud115-integration-notes.md）：
- 云端结构扁平：文件直接落 ``jav/``，不建番号目录/版本目录；Media 定位靠 pickcode，
  云端观感靠把源内相对路径编码进文件名（``ABP-123＿CD1＿movie.mp4``）。
- 多分部（VR/FC2）不做 ffmpeg 拼接：每个文件一条 Media 挂同一 movie。
- 去重按 115 全量 sha1（指纹存 ``sha1:<hex>``），显式限定本库范围。
- 两种模式均先复制，产生新 fid/pickcode → 登记以复制后 re-list 目标目录的对账结果为准；
  cleanup-source 只在整组复制、改名、入库和字幕处理完成后删除源文件。
- 幂等：中断重跑时以「目标目录 sha1 对账」收敛——已搬的跳过搬运、没改名的补改名、
  没登记的补登记。

进度事件与 ImportJob 状态流转完全对齐本地管线，前端进度页零改动。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from loguru import logger

from src.common import parse_movie_number_from_path
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.media_import_status import (
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_RENAME_FAILED,
    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_FILE_TOO_SMALL,
    FAILURE_REASON_IMPORT_JOB_CRASHED,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    FAILURE_REASON_RETRY_SOURCES_MISSING,
    FAILURE_REASON_SOURCE_DELETE_FAILED,
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    make_failure_item,
)
from src.common.runtime_time import utc_now_for_db
from src.common.subtitle_paths import movie_subtitle_root_path
from src.config.config import settings
from src.lib.cloud115 import Cloud115Client, DirEntry
from src.model import ImportJob, Media, MediaLibrary, Movie, Subtitle, get_database
from src.service.playback.cloud115_backend_service import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    find_or_create_subdir,
    require_cloud115_library,
)
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.system.resource_task_state_service import ResourceTaskStateService
from src.service.transfers.file_transfer import JAV_LIBRARY_SUBDIR
from src.service.transfers.media_import_service import MediaImportService
from src.service.transfers.tag_rules import build_media_special_tags


# 相对路径段的编码连接符：全角下划线，避开 115 文件名里常见的半角符号。
CLOUD_NAME_SEPARATOR = "＿"
# 115 文件名长度上限未见官方文档（真机验证清单项），编码名保守按 200 字符截断。
CLOUD_NAME_MAX_LENGTH = 200
# 直链下载字幕的 UA（拿链接与 GET 由 SDK 保证同 UA）。
SUBTITLE_DOWNLOAD_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Import/1.0"
# 字幕文件大小上限：.srt 纯文本，10MB 足够富余。
SUBTITLE_MAX_BYTES = 10 * 1024 * 1024

ImportProgressCallback = Callable[[dict], None]

CLOUD115_TRANSFER_MODE_COPY = "copy"
CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE = "cleanup-source"
CLOUD115_TRANSFER_MODE_LEGACY_MOVE = "move"


def normalize_cloud115_transfer_mode(transfer_mode: str) -> str:
    """兼容旧 move 输入，但所有新作业和执行路径统一使用 cleanup-source。"""
    if transfer_mode == CLOUD115_TRANSFER_MODE_LEGACY_MOVE:
        return CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
    if transfer_mode in (
        CLOUD115_TRANSFER_MODE_COPY,
        CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    ):
        return transfer_mode
    raise ValueError("invalid_transfer_mode")


@dataclass
class CloudSubtitleFile:
    """源目录里与视频配对的 .srt sidecar。"""

    fid: str
    pickcode: str
    name: str


@dataclass
class CloudSourceFile:
    """枚举 + 分拣后的单个待导入云端视频。"""

    fid: str
    pickcode: str
    name: str
    sha1: str
    size: int
    play_long: int | None
    censored: bool
    rel_dir_parts: tuple[str, ...]
    # 所在父目录 cid：字幕 sidecar 配对按同目录匹配。
    parent_cid: str = ""
    subtitle: CloudSubtitleFile | None = None

    @property
    def rel_path(self) -> str:
        """源目录内相对路径（人可读，用于失败清单与重导匹配）。"""
        return "/".join([*self.rel_dir_parts, self.name])


@dataclass
class CloudImportGroup:
    """按番号聚合后的一组待导入云端文件（不合并，逐文件登记）。"""

    movie_number: str
    files: List[CloudSourceFile] = field(default_factory=list)


def encode_cloud_file_name(
    rel_dir_parts: tuple[str, ...],
    file_name: str,
    *,
    max_length: int = CLOUD_NAME_MAX_LENGTH,
) -> str:
    """把源内相对路径编码进文件名：``ABP-123/CD1/movie.mp4`` → ``ABP-123＿CD1＿movie.mp4``。

    超长截断保尾不保头：优先丢最靠近源根的目录段（文件名 + 扩展名保持完整）；
    只剩文件名仍超长时截 stem 尾部、保扩展名。
    """
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


class Cloud115ImportService:
    """115 源目录 → cloud115 媒体库的导入编排。"""

    def __init__(self, media_import_service: MediaImportService | None = None) -> None:
        # 复用本地导入的 javdb 元数据抓取能力（线程池 worker + provider 工厂）。
        self._media_import_service = media_import_service or MediaImportService()

    # ---- 入口 ----

    def import_from_cloud115(
        self,
        library_id: int,
        source_cid: str,
        *,
        import_job_id: int | None = None,
        progress_callback: ImportProgressCallback | None = None,
        transfer_mode: str = CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
        only_files: List[str] | None = None,
    ) -> ImportJob:
        """执行一次完整的 cloud115 导入，并把中间状态写回 ImportJob。

        ``transfer_mode``: "cleanup-source"（默认，复制成功后清源）或 "copy"；旧 "move" 为别名。
        ``only_files``: 源内相对路径列表，用于失败文件的子集重导。
        """
        transfer_mode = normalize_cloud115_transfer_mode(transfer_mode)
        if not source_cid:
            raise ValueError("source_cid_required")

        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ValueError("media_library_not_found")
        require_cloud115_library(library)

        job = self._prepare_job(
            library=library,
            source_cid=source_cid,
            transfer_mode=transfer_mode,
            import_job_id=import_job_id,
        )

        failure_items: List[dict] = []
        stats = {"imported": 0, "skipped": 0, "failed": 0}
        new_playable_movies: Dict[int, dict] = {}

        job.state = IMPORT_JOB_STATE_RUNNING
        job.started_at = utc_now_for_db()
        job.save()

        try:
            asyncio.run(
                self._run(
                    library=library,
                    source_cid=source_cid,
                    transfer_mode=transfer_mode,
                    only_files=only_files,
                    failure_items=failure_items,
                    stats=stats,
                    new_playable_movies=new_playable_movies,
                    progress_callback=progress_callback,
                    job=job,
                )
            )
            job.imported_count = stats["imported"]
            job.skipped_count = stats["skipped"]
            job.failed_count = stats["failed"]
            job.state = (
                IMPORT_JOB_STATE_FAILED if stats["failed"] > 0 else IMPORT_JOB_STATE_COMPLETED
            )
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            logger.info(
                "Cloud115 import job finished job_id={} state={} imported={} skipped={} failed={}",
                job.id, job.state, job.imported_count, job.skipped_count, job.failed_count,
            )
            self._emit(
                progress_callback,
                event="job_finished",
                text="115 网盘导入任务完成",
                summary_patch=self._summary(stats, new_playable_movies),
            )
            return job
        except Exception as exc:
            # 管线整体崩溃（非单文件失败）：补任务级失败并落终态。
            failure_items.append(
                make_failure_item(job.source_path, FAILURE_REASON_IMPORT_JOB_CRASHED, str(exc))
            )
            job.imported_count = stats["imported"]
            job.skipped_count = stats["skipped"]
            job.failed_count = stats["failed"] + 1
            job.state = IMPORT_JOB_STATE_FAILED
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            logger.exception(
                "Cloud115 import job crashed job_id={} source_cid={} detail={}",
                job.id, source_cid, exc,
            )
            self._emit(
                progress_callback,
                event="job_failed",
                text="115 网盘导入任务失败",
                summary_patch=self._summary(stats, new_playable_movies),
            )
            raise

    # ---- 作业准备 ----

    @staticmethod
    def _prepare_job(
        *,
        library: MediaLibrary,
        source_cid: str,
        transfer_mode: str,
        import_job_id: int | None,
    ) -> ImportJob:
        if import_job_id is None:
            return ImportJob.create(
                source_path=f"cloud115:{source_cid}",
                source_cid=source_cid,
                library=library,
                state=IMPORT_JOB_STATE_PENDING,
                transfer_mode=transfer_mode,
            )
        job = ImportJob.get_by_id(import_job_id)
        job.source_cid = source_cid
        job.library = library
        job.state = IMPORT_JOB_STATE_PENDING
        job.transfer_mode = transfer_mode
        job.imported_count = 0
        job.skipped_count = 0
        job.failed_count = 0
        job.failed_files = "[]"
        job.started_at = None
        job.finished_at = None
        job.save()
        return job

    # ---- 异步主流程 ----

    async def _run(
        self,
        *,
        library: MediaLibrary,
        source_cid: str,
        transfer_mode: str,
        only_files: List[str] | None,
        failure_items: List[dict],
        stats: dict,
        new_playable_movies: Dict[int, dict],
        progress_callback: ImportProgressCallback | None,
        job: ImportJob,
    ) -> None:
        config = require_cloud115_library(library)
        root_cid = config["root_cid"]

        async with cloud115_client_for(library) as client:
            # 触发端已校验过；导入执行与触发之间目录结构可能变化，这里幂等兜底。
            await assert_cid_outside_library_root(
                client, source_cid=source_cid, root_cid=root_cid
            )
            # 源目录名参与番号识别（用户常选番号命名的目录本身），并落作业展示路径。
            source_meta = await client.dir_info(source_cid)
            source_display = "/".join(
                [*(crumb.name for crumb in source_meta.paths), source_meta.name]
            )
            job.source_path = source_display[:1024] or f"cloud115:{source_cid}"
            job.save()

            jav_cid = await find_or_create_subdir(
                client, parent_cid=root_cid, name=JAV_LIBRARY_SUBDIR
            )

            # 1) 枚举 + 分拣 + 去重
            groups, scan_skipped, scan_failed = await self._scan_source(
                client,
                library=library,
                source_cid=source_cid,
                source_name=source_meta.name,
                transfer_mode=transfer_mode,
                only_files=only_files,
                failure_items=failure_items,
            )
            stats["skipped"] += scan_skipped
            stats["failed"] += scan_failed
            # 子集重导所选文件全部缺失时不得静默判 completed（与本地语义一致）。
            if only_files is not None and not groups and scan_skipped == 0 and scan_failed == 0:
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        job.source_path, FAILURE_REASON_RETRY_SOURCES_MISSING,
                        "待重导的源文件均已不存在",
                    )
                )

            total_movies = len(groups)
            completed_movies = 0
            self._emit(
                progress_callback,
                event="scan_complete",
                total_movies=total_movies,
                current=0,
                total=total_movies,
                text="115 源目录扫描完成",
                summary_patch=self._summary(stats, new_playable_movies),
            )

            if not groups:
                return

            # 2) 两种模式都先复制：预扫目标 sha1，重跑复用已复制产物。
            target_entries_by_sha1 = await self._list_target_files(client, jav_cid)

            # 3) 元数据并发抓取 + 逐番号搬运登记
            with self._media_import_service.metadata_import_batch(
                [group.movie_number for group in groups],
                thread_name_prefix="cloud115-import-metadata",
            ) as metadata_futures:
                for group in groups:
                    movie_number = group.movie_number
                    self._emit(
                        progress_callback,
                        event="movie_started",
                        stage="metadata",
                        movie_number=movie_number,
                        completed_movies=completed_movies,
                        total_movies=total_movies,
                        current=completed_movies,
                        total=total_movies,
                        text=f"正在抓取影片元数据 {movie_number}",
                        summary_patch=self._summary(stats, new_playable_movies),
                    )
                    metadata_result = metadata_futures[movie_number].result()
                    if metadata_result.failure_reason is not None:
                        for cloud_file in group.files:
                            stats["failed"] += 1
                            failure_items.append(
                                make_failure_item(
                                    cloud_file.rel_path,
                                    metadata_result.failure_reason,
                                    metadata_result.failure_detail or "",
                                )
                            )
                        completed_movies += 1
                        self._emit(
                            progress_callback,
                            event="movie_finished",
                            stage="metadata",
                            movie_number=movie_number,
                            completed_movies=completed_movies,
                            total_movies=total_movies,
                            current=completed_movies,
                            total=total_movies,
                            text=f"影片元数据处理失败 {movie_number}",
                            summary_patch=self._summary(stats, new_playable_movies),
                        )
                        continue

                    movie = Movie.get_by_id(metadata_result.movie_id)
                    self._emit(
                        progress_callback,
                        event="movie_stage",
                        stage="import-media",
                        movie_number=movie_number,
                        completed_movies=completed_movies,
                        total_movies=total_movies,
                        current=completed_movies,
                        total=total_movies,
                        text=f"正在搬运影片文件 {movie_number}",
                        summary_patch=self._summary(stats, new_playable_movies),
                    )

                    await self._import_group(
                        client,
                        library=library,
                        movie=movie,
                        group=group,
                        jav_cid=jav_cid,
                        transfer_mode=transfer_mode,
                        target_entries_by_sha1=target_entries_by_sha1,
                        failure_items=failure_items,
                        stats=stats,
                        new_playable_movies=new_playable_movies,
                    )

                    completed_movies += 1
                    self._emit(
                        progress_callback,
                        event="movie_finished",
                        stage="import-media",
                        movie_number=movie_number,
                        completed_movies=completed_movies,
                        total_movies=total_movies,
                        current=completed_movies,
                        total=total_movies,
                        text=f"影片导入完成 {movie_number}",
                        summary_patch=self._summary(stats, new_playable_movies),
                    )

    # ---- 枚举 / 分拣 ----

    async def _scan_source(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        source_cid: str,
        source_name: str,
        transfer_mode: str,
        only_files: List[str] | None,
        failure_items: List[dict],
    ) -> tuple[List[CloudImportGroup], int, int]:
        """枚举源目录树 → 分拣视频/字幕 → 番号识别 → sha1 去重，产出按番号聚合的分组。"""
        minimum_size = settings.media.allowed_min_video_file_size
        skipped_count = 0
        failed_count = 0
        only_set = set(only_files) if only_files is not None else None

        # 目录名映射（递归列文件模式每条只带 parent cid，父目录名靠这里回溯）。
        dir_map = await self._build_dir_map(client, source_cid)

        videos: List[CloudSourceFile] = []
        subtitles_by_dir: Dict[str, List[CloudSubtitleFile]] = {}
        async for entry in client.iter_files_recursive(source_cid):
            suffix = ("." + entry.name.rsplit(".", 1)[-1].lower()) if "." in entry.name else ""
            rel_dir_parts = self._rel_dir_parts(entry.parent_id, dir_map, source_cid)
            if suffix == ".srt":
                subtitles_by_dir.setdefault(entry.parent_id, []).append(
                    CloudSubtitleFile(fid=entry.entry_id, pickcode=entry.pickcode, name=entry.name)
                )
                continue
            if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
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

        # 字幕 sidecar 配对：同目录 + stem 相同或 "stem." 前缀（与本地扫描规则一致）。
        for video in videos:
            video_stem = video.name.rsplit(".", 1)[0].lower()
            for candidate in subtitles_by_dir.get(video.parent_cid, []):
                candidate_stem = candidate.name.rsplit(".", 1)[0].lower()
                if candidate_stem == video_stem or candidate_stem.startswith(f"{video_stem}."):
                    video.subtitle = candidate
                    break

        # 番号识别：喂「源目录名/相对路径」，识别函数取最后两级（覆盖番号在目录名的情形）。
        grouped: Dict[str, CloudImportGroup] = {}
        seen_sha1: set[str] = set()
        for video in videos:
            recognition_input = "/".join([source_name, video.rel_path])
            movie_number = parse_movie_number_from_path(recognition_input)
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
            existing = self._find_library_media(library, video.sha1, valid=True)
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

    @staticmethod
    async def _build_dir_map(
        client: Cloud115Client, source_cid: str
    ) -> Dict[str, tuple[str, str]]:
        """BFS 源目录树的目录结构，产出 cid -> (目录名, 父 cid) 映射（目录数远小于文件数）。"""
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

    @staticmethod
    def _rel_dir_parts(
        parent_cid: str, dir_map: Dict[str, tuple[str, str]], source_cid: str
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

    # ---- 搬运 / 对账 / 登记 ----

    @staticmethod
    async def _list_target_files(
        client: Cloud115Client, jav_cid: str
    ) -> Dict[str, List[DirEntry]]:
        """列目标 jav/ 目录（扁平一层）的全部文件，按 sha1 归组，供 copy 对账。"""
        by_sha1: Dict[str, List[DirEntry]] = {}
        offset = 0
        while True:
            entries, total = await client.list_dir(jav_cid, offset=offset, limit=1150)
            for entry in entries:
                if not entry.is_dir and entry.sha1:
                    by_sha1.setdefault(entry.sha1.upper(), []).append(entry)
            offset += len(entries)
            if not entries or offset >= total:
                break
        return by_sha1

    async def _import_group(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        movie: Movie,
        group: CloudImportGroup,
        jav_cid: str,
        transfer_mode: str,
        target_entries_by_sha1: Dict[str, List[DirEntry]],
        failure_items: List[dict],
        stats: dict,
        new_playable_movies: Dict[int, dict],
    ) -> None:
        """复制并登记一个番号组；任一改名失败时整组不入库、不清源。"""
        # 1) 两种模式都复制；目标已有同 sha1 表示上次已完成复制，直接复用。
        preexisting_target_sha1 = set(target_entries_by_sha1)
        pending_transfer = [
            cloud_file
            for cloud_file in group.files
            if cloud_file.sha1 not in target_entries_by_sha1
        ]
        try:
            if pending_transfer:
                fids = [cloud_file.fid for cloud_file in pending_transfer]
                await client.copy_files(fids, pid=jav_cid)
                # 复制产生新 fid/pickcode → re-list 对账刷新。
                target_entries_by_sha1.clear()
                target_entries_by_sha1.update(
                    await self._list_target_files(client, jav_cid)
                )
        except Exception as exc:
            self._record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=str(exc),
                failure_items=failure_items,
                stats=stats,
            )
            logger.exception(
                "Cloud115 copy failed movie_number={} mode={} detail={}",
                group.movie_number, transfer_mode, exc,
            )
            return

        # 2) 按 sha1 对账每个复制产物。
        resolved: List[tuple[CloudSourceFile, str, str, str, str]] = []
        for cloud_file in group.files:
            encoded_name = encode_cloud_file_name(cloud_file.rel_dir_parts, cloud_file.name)
            existing_valid = self._find_library_media(library, cloud_file.sha1, valid=True)
            target_entry = None
            reuse_managed_target = (
                existing_valid is not None
                and cloud_file.sha1 in preexisting_target_sha1
            )
            if reuse_managed_target:
                target_entry = self._resolve_registered_entry(
                    target_entries_by_sha1.get(cloud_file.sha1) or [],
                    existing_valid,
                )
            if target_entry is None:
                target_entry = self._resolve_copied_entry(
                    target_entries_by_sha1, cloud_file, encoded_name
                )
            if target_entry is None:
                self._record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    detail=f"复制后在目标目录未找到 sha1={cloud_file.sha1} 的条目",
                    failure_items=failure_items,
                    stats=stats,
                )
                return
            resolved.append(
                (
                    cloud_file,
                    target_entry.entry_id,
                    target_entry.pickcode,
                    target_entry.name,
                    # cleanup-source 的正常重复不应改名已有受管文件；只核验/对账后清理来源。
                    target_entry.name if reuse_managed_target else encoded_name,
                )
            )

        # 3) 逐文件改名；每次请求后按 fid 查询实际名称，失败立即终止整组。
        for cloud_file, target_fid, _pickcode, current_name, target_name in resolved:
            if current_name == target_name:
                continue
            try:
                await client.rename_file(target_fid, target_name)
                await self._verify_renamed_file(client, target_fid, target_name)
            except Exception as exc:
                self._record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_RENAME_FAILED,
                    detail=f"fid={target_fid}: {exc}",
                    failure_items=failure_items,
                    stats=stats,
                )
                logger.warning(
                    "Cloud115 rename verification failed movie_number={} fid={} detail={}",
                    group.movie_number, target_fid, exc,
                )
                return

        # 4) 整组 Media 在同一事务登记；任一失败回滚整组。
        registration_results: List[tuple[CloudSourceFile, bool]] = []
        try:
            with get_database().atomic():
                for cloud_file, target_fid, target_pickcode, _current_name, target_name in resolved:
                    registered = self._register_media(
                        library=library,
                        movie=movie,
                        cloud_file=cloud_file,
                        target_fid=target_fid,
                        target_pickcode=target_pickcode,
                        encoded_name=target_name,
                    )
                    registration_results.append((cloud_file, registered))
        except Exception as exc:
            self._record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=str(exc),
                failure_items=failure_items,
                stats=stats,
            )
            logger.exception(
                "Cloud115 media register failed movie_number={} detail={}",
                group.movie_number, exc,
            )
            return

        for cloud_file, registered in registration_results:
            if cloud_file.censored:
                # 违规文件按 invalid 登记完成，报告但不计入可播放。
                stats["imported"] += 1
                failure_items.append(
                    make_failure_item(
                        cloud_file.rel_path, FAILURE_REASON_CLOUD115_FILE_CENSORED,
                        "115 标记该文件违规（ic=1）",
                    )
                )
            elif registered:
                stats["imported"] += 1
                new_playable_movies[movie.id] = {
                    "movie_id": movie.id,
                    "movie_number": movie.movie_number,
                    "title": movie.title,
                }
            else:
                stats["skipped"] += 1

        # 5) 字幕全部处理完才允许 cleanup-source。清源模式下字幕失败作为可重导文件失败。
        subtitles_ready = True
        for cloud_file, _target_fid, _target_pickcode, _current_name, target_name in resolved:
            if cloud_file.subtitle is not None:
                try:
                    await self._import_subtitle(
                        client,
                        movie=movie,
                        cloud_file=cloud_file,
                        encoded_name=target_name,
                    )
                except Exception as exc:
                    item = make_failure_item(
                        cloud_file.rel_path,
                        FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                        str(exc),
                    )
                    if transfer_mode == CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE:
                        item["kind"] = "file"
                        stats["failed"] += 1
                        subtitles_ready = False
                    failure_items.append(item)
                    logger.warning(
                        "Cloud115 subtitle download failed movie_number={} rel_path={} detail={}",
                        group.movie_number, cloud_file.rel_path, exc,
                    )

        if transfer_mode != CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE or not subtitles_ready:
            return

        # 6) 清源严格放在复制、改名验证、入库、字幕之后；逐文件删除便于失败后精确重试。
        for cloud_file, _target_fid, _target_pickcode, _current_name, _encoded_name in resolved:
            try:
                if cloud_file.subtitle is not None:
                    await client.delete_files([cloud_file.subtitle.fid])
                await client.delete_files([cloud_file.fid])
            except Exception as exc:
                stats["failed"] += 1
                item = make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_SOURCE_DELETE_FAILED,
                    str(exc),
                )
                # cloud115 清源失败必须可以从原 source_cid 精确重试。
                item["kind"] = "file"
                failure_items.append(item)
                logger.warning(
                    "Cloud115 source delete failed movie_number={} rel_path={} detail={}",
                    group.movie_number, cloud_file.rel_path, exc,
                )

    @staticmethod
    def _record_group_failure(
        group: CloudImportGroup,
        *,
        reason: str,
        detail: str,
        failure_items: List[dict],
        stats: dict,
    ) -> None:
        for cloud_file in group.files:
            stats["failed"] += 1
            failure_items.append(make_failure_item(cloud_file.rel_path, reason, detail))

    @staticmethod
    async def _verify_renamed_file(
        client: Cloud115Client,
        fid: str,
        expected_name: str,
    ) -> None:
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

    @staticmethod
    def _resolve_copied_entry(
        target_entries_by_sha1: Dict[str, List[DirEntry]],
        cloud_file: CloudSourceFile,
        encoded_name: str,
    ) -> DirEntry | None:
        """复制对账：目标目录同 sha1 条目中优先挑名字匹配的（原名或编码名）。"""
        candidates = target_entries_by_sha1.get(cloud_file.sha1) or []
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.name in (cloud_file.name, encoded_name):
                return candidate
        return candidates[0]

    @staticmethod
    def _resolve_registered_entry(candidates: List[DirEntry], media: Media) -> DirEntry | None:
        """优先按数据库已有 fid/pickcode 找回受管目标，避免同 SHA 条目之间误切换。"""
        locator = media.backend_locator or {}
        fid = str(locator.get("fid") or "")
        pickcode = str(locator.get("pickcode") or "")
        for candidate in candidates:
            if fid and candidate.entry_id == fid:
                return candidate
        for candidate in candidates:
            if pickcode and candidate.pickcode == pickcode:
                return candidate
        return None

    def _register_media(
        self,
        *,
        library: MediaLibrary,
        movie: Movie,
        cloud_file: CloudSourceFile,
        target_fid: str,
        target_pickcode: str,
        encoded_name: str,
    ) -> bool:
        """按 sha1 指纹幂等登记一条 cloud115 Media；返回是否新登记（False = 已存在跳过）。"""
        fingerprint = f"sha1:{cloud_file.sha1}"
        # locator 键序固定（fid/pickcode/name/source_path），(library, locator) 唯一索引依赖此序。
        locator = {
            "fid": target_fid,
            "pickcode": target_pickcode,
            "name": encoded_name,
            "source_path": cloud_file.rel_path,
        }
        special_tags = build_media_special_tags(
            [cloud_file.rel_path],
            movie.movie_number,
            video_info=None,
            has_subtitle=cloud_file.subtitle is not None,
        )
        valid = not cloud_file.censored

        existing_valid = self._find_library_media(library, cloud_file.sha1, valid=True)
        if existing_valid is not None:
            # 目标可能由上次中断重跑生成，或用户删除旧目标后由本次重新复制；先把 locator
            # 对账到实际条目，成功落库后 cleanup-source 才能安全删除来源。
            previous_locator = existing_valid.backend_locator or {}
            locator["source_path"] = previous_locator.get("source_path") or cloud_file.rel_path
            existing_valid.backend_locator = locator
            existing_valid.file_size_bytes = cloud_file.size
            if cloud_file.play_long:
                existing_valid.duration_seconds = cloud_file.play_long
            existing_valid.updated_at = utc_now_for_db()
            existing_valid.save()
            return False
        invalid_media = self._find_library_media(library, cloud_file.sha1, valid=False)
        if invalid_media is not None:
            # 复活：同内容曾登记过又失效（远端删除后重新出现），更新定位与归属。
            invalid_media.movie = movie
            invalid_media.library = library
            invalid_media.backend_locator = locator
            invalid_media.file_size_bytes = cloud_file.size
            if cloud_file.play_long:
                invalid_media.duration_seconds = cloud_file.play_long
            invalid_media.special_tags = special_tags
            invalid_media.valid = valid
            invalid_media.updated_at = utc_now_for_db()
            invalid_media.save()
            if valid:
                self._reset_thumbnail_state(invalid_media.id)
            return True

        media = Media.create(
            movie=movie,
            library=library,
            backend_locator=locator,
            content_fingerprint=fingerprint,
            file_size_bytes=cloud_file.size,
            duration_seconds=cloud_file.play_long or 0,
            special_tags=special_tags,
            valid=valid,
        )
        if valid:
            self._reset_thumbnail_state(media.id)
        logger.info(
            "Cloud115 media registered movie_number={} media_id={} pickcode={} name={}",
            movie.movie_number, media.id, target_pickcode, encoded_name,
        )
        return True

    @staticmethod
    def _find_library_media(library: MediaLibrary, sha1: str, *, valid: bool) -> Media | None:
        return (
            Media.select()
            .where(
                Media.library == library,
                Media.content_fingerprint == f"sha1:{sha1}",
                Media.valid == valid,
            )
            .order_by(Media.id.desc())
            .first()
        )

    @staticmethod
    def _reset_thumbnail_state(media_id: int) -> None:
        # 与本地导入一致：新登记/复活的媒体，缩略图任务回到全新待处理状态。
        ResourceTaskStateService.reset_for_requeue(MediaThumbnailService.TASK_KEY, media_id)

    async def _import_subtitle(
        self,
        client: Cloud115Client,
        *,
        movie: Movie,
        cloud_file: CloudSourceFile,
        encoded_name: str,
    ) -> None:
        """把配对的 .srt 下载到 subtitle_root/{番号}/ 并登记 Subtitle。

        字幕不复制到 115（库子树只存影片文件）；删除源字幕由整组成功后的清源阶段统一处理。
        """
        subtitle = cloud_file.subtitle
        assert subtitle is not None
        subtitle_dir = movie_subtitle_root_path(movie.movie_number)
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        # 文件名跟随编码后的视频名，保证同番号多版本字幕可区分；重名冲突加 fid 后缀。
        encoded_stem = encoded_name.rsplit(".", 1)[0]
        target_path = subtitle_dir / f"{encoded_stem}.srt"
        existing = Subtitle.get_or_none(
            (Subtitle.movie == movie) & (Subtitle.file_path == str(target_path))
        )
        if existing is not None and target_path.exists():
            return
        if target_path.exists():
            target_path = subtitle_dir / f"{encoded_stem}-{subtitle.fid}.srt"
            existing = Subtitle.get_or_none(
                (Subtitle.movie == movie) & (Subtitle.file_path == str(target_path))
            )
            if existing is not None and target_path.exists():
                return
        content = await client.download_bytes(
            subtitle.pickcode,
            user_agent=SUBTITLE_DOWNLOAD_UA,
            max_bytes=SUBTITLE_MAX_BYTES,
        )
        target_path.write_bytes(content)
        Subtitle.get_or_create(movie=movie, file_path=str(target_path))

    # ---- 进度 ----

    @staticmethod
    def _summary(stats: dict, new_playable_movies: Dict[int, dict]) -> dict:
        return {
            "imported_count": stats["imported"],
            "skipped_count": stats["skipped"],
            "failed_count": stats["failed"],
            "new_playable_movies": list(new_playable_movies.values()),
        }

    @staticmethod
    def _emit(progress_callback: ImportProgressCallback | None, **payload: object) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)
