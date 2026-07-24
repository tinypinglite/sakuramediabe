"""cloud115 复制式导入管线（JAV）。

与本地 ``MediaImportService.import_from_source`` 对称的云端版本：用户指定 115 源目录
（cid），管线把其中的 JAV 视频复制进库管理目录 ``sakuramedia/jav/``（copy 或
cleanup-source），字幕 ``.srt`` 下载到本地 ``movies/<shard>/{番号}/subtitles/``，最后登记 Media
（path 为空、backend_locator 定位）。

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
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple

from loguru import logger

from src.common import parse_movie_number_from_path, subtitle_matches_movie_number
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.media_import_status import (
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
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
from src.common.media_paths import movie_subtitle_dir
from src.config.config import settings
from src.lib.cloud115 import Cloud115Client, DirEntry
from src.model import ImportJob, Media, MediaLibrary, Movie, Subtitle, get_database
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    find_or_create_subdir,
    require_cloud115_library,
)
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.file_transfer import JAV_LIBRARY_SUBDIR
from src.service.transfers.cloud115_import_common import (
    CLOUD115_METADATA_PROBE_MAX_BYTES,
    CLOUD115_METADATA_PROBE_UA,
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    CLOUD115_TRANSFER_MODE_COPY,
    CLOUD115_TRANSFER_MODE_LEGACY_MOVE,
    build_cloud115_dir_map,
    cloud115_rel_dir_parts,
    create_cloud115_version_subdir,
    list_cloud115_entity_target_files,
    list_cloud115_target_files,
    normalize_cloud115_transfer_mode,
    normalize_jav_media_filename,
    probe_cloud115_media,
    resolve_cloud115_copied_entry,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115_media_registrar import Cloud115MediaRegistrar
from src.service.transfers.media_import_service import MediaImportService
from src.service.transfers.tag_rules import build_media_special_tags


# 直链下载字幕的 UA（拿链接与 GET 由 SDK 保证同 UA）。
SUBTITLE_DOWNLOAD_UA = "Mozilla/5.0 SakuraMedia-Cloud115-Import/1.0"
# 字幕文件大小上限：.srt 纯文本，10MB 足够富余。
SUBTITLE_MAX_BYTES = 10 * 1024 * 1024
ImportProgressCallback = Callable[[dict], None]


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
    # 番号识别结果：配对与分组共用，避免重复解析（videos 域复用本 dataclass 时保持空串）。
    movie_number: str = ""
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


class _ResolvedFile(NamedTuple):
    """_import_group 中 copy + 对账后的单个待登记条目。"""

    cloud_file: CloudSourceFile
    target_fid: str
    target_pickcode: str
    current_name: str
    target_name: str


class Cloud115ImportService:
    """115 源目录 → cloud115 媒体库的导入编排。"""

    def __init__(
        self,
        media_import_service: MediaImportService | None = None,
        media_metadata_probe_service: MediaMetadataProbeService | None = None,
    ) -> None:
        # 复用本地导入的 javdb 元数据抓取能力（线程池 worker + provider 工厂）。
        self._media_import_service = media_import_service or MediaImportService()
        self._media_metadata_probe_service = (
            media_metadata_probe_service or MediaMetadataProbeService()
        )

    # 兼容现有调用与测试；实现统一下沉到无 JAV 业务归属的公共模块。
    _build_dir_map = staticmethod(build_cloud115_dir_map)
    _rel_dir_parts = staticmethod(cloud115_rel_dir_parts)
    _list_target_files = staticmethod(list_cloud115_target_files)
    _resolve_copied_entry = staticmethod(resolve_cloud115_copied_entry)
    _verify_renamed_file = staticmethod(verify_cloud115_renamed_file)

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

            # 2) 元数据并发抓取 + 逐番号搬运登记（目标目录/版本目录按番号在 _import_group 里建）
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

        # 番号识别前置：配对与分组共用识别结果，避免重复解析。
        # 喂「源目录名/相对路径」，识别函数取最后两级（覆盖番号在目录名的情形）。
        for video in videos:
            video.movie_number = parse_movie_number_from_path(
                "/".join([source_name, video.rel_path])
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
        grouped: Dict[str, CloudImportGroup] = {}
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

    # ---- 搬运 / 对账 / 登记 ----

    async def _import_group(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        movie: Movie,
        group: CloudImportGroup,
        jav_cid: str,
        transfer_mode: str,
        failure_items: List[dict],
        stats: dict,
        new_playable_movies: Dict[int, dict],
    ) -> None:
        """复制并登记一个番号组；任一改名失败时整组不入库、不清源。

        目标结构对齐本地：``jav/{番号}/{版本ms}/{番号}{ext}``。同番号多分部共享
        实体目录，各自版本子目录；中断恢复时按番号目录做 sha1 对账，命中即复用远端
        已复制条目，不重复复制。
        """
        try:
            entity_cid = await find_or_create_subdir(
                client, parent_cid=jav_cid, name=group.movie_number
            )
        except Exception as exc:
            self._record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=f"创建番号目录失败: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.exception(
                "Cloud115 entity dir failed movie_number={} detail={}",
                group.movie_number, exc,
            )
            return

        # 1) 预扫番号目录下所有版本子目录里的 sha1，用于中断恢复对账。
        try:
            entity_entries_by_sha1 = await list_cloud115_entity_target_files(
                client, entity_cid
            )
        except Exception as exc:
            self._record_group_failure(
                group,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=f"列出番号目录失败: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.exception(
                "Cloud115 entity list failed movie_number={} detail={}",
                group.movie_number, exc,
            )
            return
        preexisting_target_sha1 = set(entity_entries_by_sha1)

        # 1b) 主动清理多余候选：同一 sha1 在实体目录下有多个副本，说明是历史 job 崩溃
        # 或重跑遗留的脏数据。保留 mtime 最新的作为权威条目，其余进回收站；
        # 删失败仅告警不中断本次导入（下次运行时可再次尝试）。
        # entity 目录完全由 SakuraMedia 管，用户不会往里塞文件，主动收敛安全。
        for sha1, candidates in list(entity_entries_by_sha1.items()):
            if len(candidates) <= 1:
                continue
            winner = max(candidates, key=lambda entry: entry.mtime)
            stale = [entry for entry in candidates if entry.entry_id != winner.entry_id]
            entity_entries_by_sha1[sha1] = [winner]
            stale_fids = [entry.entry_id for entry in stale]
            try:
                await client.delete_files(stale_fids)
                logger.warning(
                    "Cloud115 entity stale duplicates cleaned movie_number={} sha1={} kept_fid={} kept_mtime={} deleted_fids={}",
                    group.movie_number, sha1, winner.entry_id, winner.mtime, stale_fids,
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "Cloud115 entity stale duplicates cleanup failed movie_number={} sha1={} stale_fids={} detail={}",
                    group.movie_number, sha1, stale_fids, cleanup_exc,
                )

        # 2) 逐文件处理：sha1 已在实体下则复用；否则新建版本目录 + 复制。
        resolved: List[_ResolvedFile] = []
        for cloud_file in group.files:
            target_name = normalize_jav_media_filename(
                group.movie_number, cloud_file.name
            )
            existing_valid = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=True)
            reuse_managed_target = (
                existing_valid is not None
                and cloud_file.sha1 in preexisting_target_sha1
            )
            target_entry: DirEntry | None = None
            if cloud_file.sha1 in entity_entries_by_sha1:
                candidates = entity_entries_by_sha1[cloud_file.sha1]
                if reuse_managed_target:
                    target_entry = self._resolve_registered_entry(
                        candidates, existing_valid
                    )
                target_entry = target_entry or candidates[0]
            else:
                # 需要新复制：为本文件建独立版本目录，跟本地 create_version_directory 对齐。
                version_cid: str | None = None
                try:
                    version_cid = await create_cloud115_version_subdir(
                        client,
                        parent_cid=entity_cid,
                        now_ms=int(time.time() * 1000),
                    )
                    await client.copy_files([cloud_file.fid], pid=version_cid)
                    version_entries = await self._list_target_files(client, version_cid)
                except Exception as exc:
                    # copy/list 失败时，version_cid 是本次新建的独占目录，回收避免
                    # 在 jav/{番号}/ 下累积空/半成品目录（走回收站有缓冲）。
                    # entity_cid 可能被同番号其它导入共享，此处不动。
                    if version_cid is not None:
                        try:
                            await client.delete_files([version_cid])
                        except Exception as cleanup_exc:
                            logger.warning(
                                "Cloud115 version dir rollback failed movie_number={} version_cid={} detail={}",
                                group.movie_number, version_cid, cleanup_exc,
                            )
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
                target_entry = self._resolve_copied_entry(
                    version_entries, cloud_file, target_name
                )
                if target_entry is not None:
                    # 让同批次后续同 sha1 命中能看到刚复制的条目，避免二次 copy。
                    entity_entries_by_sha1[cloud_file.sha1] = [target_entry]

            if target_entry is None:
                self._record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                    detail=f"复制后在目标目录未找到 sha1={cloud_file.sha1} 的条目",
                    failure_items=failure_items,
                    stats=stats,
                )
                return
            resolved.append(_ResolvedFile(
                cloud_file=cloud_file,
                target_fid=target_entry.entry_id,
                target_pickcode=target_entry.pickcode,
                current_name=target_entry.name,
                # 复用已登记条目时保留原名，不改动已在库的命名；新复制统一规范名。
                target_name=target_entry.name if reuse_managed_target else target_name,
            ))

        # 3) 逐文件改名；每次请求后按 fid 查询实际名称，失败立即终止整组。
        for r in resolved:
            if r.current_name == r.target_name:
                continue
            try:
                await client.rename_file(r.target_fid, r.target_name)
                await self._verify_renamed_file(client, r.target_fid, r.target_name)
            except Exception as exc:
                self._record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_RENAME_FAILED,
                    detail=f"fid={r.target_fid}: {exc}",
                    failure_items=failure_items,
                    stats=stats,
                )
                logger.warning(
                    "Cloud115 rename verification failed movie_number={} fid={} detail={}",
                    group.movie_number, r.target_fid, exc,
                )
                return

        # 4) 在数据库事务外探测最终受管文件。有效媒体必须带完整技术元数据入库；
        # 已登记且已有 video_info 的清源重试直接复用，避免重复读取远端文件。
        probe_results: Dict[str, MediaMetadataProbeResult | None] = {}
        for r in resolved:
            existing_valid = Cloud115MediaRegistrar.find_library_media(library, r.cloud_file.sha1, valid=True)
            if r.cloud_file.censored or (
                existing_valid is not None and existing_valid.video_info is not None
            ):
                probe_results[r.cloud_file.sha1] = None
                continue
            try:
                probe_results[r.cloud_file.sha1] = await self._probe_cloud115_media(
                    client,
                    pickcode=r.target_pickcode,
                    file_size_bytes=r.cloud_file.size,
                )
            except Exception as exc:
                self._record_group_failure(
                    group,
                    reason=FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
                    detail=f"{r.cloud_file.rel_path}: {exc}",
                    failure_items=failure_items,
                    stats=stats,
                )
                logger.warning(
                    "Cloud115 metadata probe failed movie_number={} rel_path={} detail={}",
                    group.movie_number, r.cloud_file.rel_path, exc,
                )
                return

        # 5) 整组 Media 在同一事务登记；任一失败回滚整组。
        registration_results: List[tuple[CloudSourceFile, bool]] = []
        try:
            with get_database().atomic():
                for r in resolved:
                    registered = self._register_media(
                        library=library,
                        movie=movie,
                        cloud_file=r.cloud_file,
                        target_fid=r.target_fid,
                        target_pickcode=r.target_pickcode,
                        encoded_name=r.target_name,
                        metadata=probe_results[r.cloud_file.sha1],
                    )
                    registration_results.append((r.cloud_file, registered))
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

        # 6) 字幕全部处理完才允许 cleanup-source。清源模式下字幕失败作为可重导文件失败。
        subtitles_ready = True
        for r in resolved:
            if r.cloud_file.subtitle is not None:
                try:
                    await self._import_subtitle(
                        client,
                        movie=movie,
                        cloud_file=r.cloud_file,
                        encoded_name=r.target_name,
                    )
                except Exception as exc:
                    item = make_failure_item(
                        r.cloud_file.rel_path,
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
                        group.movie_number, r.cloud_file.rel_path, exc,
                    )

        if transfer_mode != CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE or not subtitles_ready:
            return

        # 7) 清源严格放在复制、改名验证、探测、入库、字幕之后；逐文件删除便于失败后精确重试。
        for r in resolved:
            try:
                if r.cloud_file.subtitle is not None:
                    await client.delete_files([r.cloud_file.subtitle.fid])
                await client.delete_files([r.cloud_file.fid])
            except Exception as exc:
                stats["failed"] += 1
                item = make_failure_item(
                    r.cloud_file.rel_path,
                    FAILURE_REASON_SOURCE_DELETE_FAILED,
                    str(exc),
                )
                item["kind"] = "file"
                failure_items.append(item)
                logger.warning(
                    "Cloud115 source delete failed movie_number={} rel_path={} detail={}",
                    group.movie_number, r.cloud_file.rel_path, exc,
                )

    async def _probe_cloud115_media(
        self,
        client: Cloud115Client,
        *,
        pickcode: str,
        file_size_bytes: int,
    ) -> MediaMetadataProbeResult:
        metadata, fetched_bytes = await probe_cloud115_media(
            client,
            self._media_metadata_probe_service,
            pickcode=pickcode,
            file_size_bytes=file_size_bytes,
        )
        logger.info(
            "Cloud115 metadata probed pickcode={} fetched_bytes={} budget_bytes={}",
            pickcode, fetched_bytes, CLOUD115_METADATA_PROBE_MAX_BYTES,
        )
        return metadata

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
        metadata: MediaMetadataProbeResult | None,
    ) -> bool:
        """按 sha1 指纹幂等登记一条 cloud115 Media；返回是否新登记（False = 已存在跳过）。"""
        locator = Cloud115MediaRegistrar.build_locator(
            fid=target_fid,
            pickcode=target_pickcode,
            name=encoded_name,
            source_path=cloud_file.rel_path,
        )
        fingerprint = Cloud115MediaRegistrar.build_fingerprint(cloud_file.sha1)
        valid = not cloud_file.censored

        existing_valid = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=True)
        effective_video_info = metadata.video_info if metadata is not None else None
        if existing_valid is not None and effective_video_info is None:
            effective_video_info = existing_valid.video_info
        if valid and effective_video_info is None:
            raise RuntimeError("valid cloud115 media requires video_info")
        resolution = metadata.resolution if metadata is not None else None
        if metadata is not None:
            duration_seconds = metadata.duration_seconds or cloud_file.play_long or 0
        elif existing_valid is not None:
            duration_seconds = existing_valid.duration_seconds or cloud_file.play_long or 0
        else:
            duration_seconds = cloud_file.play_long or 0
        special_tags = build_media_special_tags(
            [cloud_file.rel_path],
            movie.movie_number,
            video_info=effective_video_info,
            has_subtitle=cloud_file.subtitle is not None,
        )

        if existing_valid is not None:
            previous_locator = existing_valid.backend_locator or {}
            locator["source_path"] = previous_locator.get("source_path") or cloud_file.rel_path
            Cloud115MediaRegistrar.apply_cloud115_fields(
                existing_valid,
                library=library,
                locator=locator,
                fingerprint=fingerprint,
                file_size_bytes=cloud_file.size,
                resolution=resolution,
                duration_seconds=duration_seconds,
                video_info=effective_video_info,
                special_tags=special_tags,
            )
            existing_valid.save()
            return False

        invalid_media = Cloud115MediaRegistrar.find_library_media(library, cloud_file.sha1, valid=False)
        if invalid_media is not None:
            invalid_media.movie = movie
            Cloud115MediaRegistrar.apply_cloud115_fields(
                invalid_media,
                library=library,
                locator=locator,
                fingerprint=fingerprint,
                file_size_bytes=cloud_file.size,
                resolution=resolution,
                duration_seconds=duration_seconds,
                video_info=effective_video_info,
                special_tags=special_tags,
                valid=valid,
            )
            invalid_media.save()
            if valid:
                Cloud115MediaRegistrar.reset_thumbnail_state(invalid_media.id)
            return True

        media = Cloud115MediaRegistrar.create_cloud115_media(
            movie=movie,
            library=library,
            locator=locator,
            fingerprint=fingerprint,
            file_size_bytes=cloud_file.size,
            resolution=resolution,
            duration_seconds=duration_seconds,
            video_info=effective_video_info,
            special_tags=special_tags,
            valid=valid,
        )
        if valid:
            Cloud115MediaRegistrar.reset_thumbnail_state(media.id)
        logger.info(
            "Cloud115 media registered movie_number={} media_id={} pickcode={} name={}",
            movie.movie_number, media.id, target_pickcode, encoded_name,
        )
        return True

    async def _import_subtitle(
        self,
        client: Cloud115Client,
        *,
        movie: Movie,
        cloud_file: CloudSourceFile,
        encoded_name: str,
    ) -> None:
        """把配对的 .srt 下载到 movies/<shard>/{番号}/subtitles/ 并登记 Subtitle。

        字幕不复制到 115（库子树只存影片文件）；删除源字幕由整组成功后的清源阶段统一处理。
        """
        subtitle = cloud_file.subtitle
        assert subtitle is not None
        subtitle_dir = movie_subtitle_dir(movie.movie_number)
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
