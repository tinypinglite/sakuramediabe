"""cloud115 导入管线（JAV）。

与本地 ``MediaImportService.import_from_source`` 对称的云端版本：用户指定 115 源目录
（cid），管线把其中的 JAV 视频搬进库管理目录 ``sakuramedia/jav/``（copy 或
cleanup-source），字幕 ``.srt`` 下载到本地 ``movies/<shard>/{番号}/subtitles/{番号}-<N>.srt``
（命名与本地导入 / 迁移共用一套分配器），最后登记 Media（path 为空、backend_locator 定位）。

与本地管线的关键差异（依据 docs/development/cloud115-integration-notes.md）：
- 多分部（VR/FC2）不做 ffmpeg 拼接：每个文件一条 Media 挂同一 movie。
- 去重按 115 全量 sha1（指纹存 ``sha1:<hex>``），显式限定本库范围。
- 两种模式的搬运语义不同：
  * ``copy``：复制产生新 fid/pickcode，登记以复制后 re-list 目标目录的对账结果为准，
    源文件始终保留。幂等靠「目标目录 sha1 对账」收敛——已搬的跳过搬运、没改名的补改名、
    没登记的补登记。
  * ``cleanup-source``：直接 ``files/move`` 把源搬进库，fid/pickcode 不变，因此不占双倍
    空间、不需要 re-list 对账，也没有"复制完再删源"这一步。登记**先于**搬运（Media 只靠
    pickcode 定位、与所在目录无关），所以中断最多留下"已登记但还没搬走"的源文件，重跑时
    按 locator.fid 命中并补完搬运即收敛。

进度事件与 ImportJob 状态流转完全对齐本地管线，前端进度页零改动。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable

from loguru import logger

from src.common.media_import_status import (
    FAILED_FILE_KIND_FILE,
    FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_CLOUD115_FILE_CENSORED,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
    FAILURE_REASON_CLOUD115_RENAME_FAILED,
    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_IMPORT_JOB_CRASHED,
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
from src.model import ImportJob, Media, MediaLibrary, Movie, get_database
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    require_cloud115_library,
)
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
    MediaMetadataProbeService,
)
from src.service.transfers.cloud115.importer.common import (
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    Cloud115TargetDirCache,
    Cloud115TargetDirResolver,
    list_cloud115_entity_target_files,
    list_cloud115_target_files,
    normalize_cloud115_transfer_mode,
    normalize_jav_media_filename,
    resolve_cloud115_copied_entry,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115.importer.media_registrar import Cloud115MediaRegistrar
from src.service.transfers.cloud115.importer.scanner import scan_cloud115_source
from src.service.transfers.cloud115.importer.strategies.common import (
    import_subtitle,
    probe_cloud115_media,
    record_files_failure,
    register_media,
)
from src.service.transfers.cloud115.importer.types import (
    CloudImportGroup,
    CloudSourceFile,
    ResolvedFile as _ResolvedFile,
)
from src.service.transfers.imports.import_service import MediaImportService

# 手动批量 JAV 导入在番号之间加入随机停顿，降低长时间持续请求 115 的频率。
MANUAL_GROUP_REST_MIN_SECONDS = 10.0
MANUAL_GROUP_REST_MAX_SECONDS = 30.0
ImportProgressCallback = Callable[[dict], None]


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

    # 兼容现有调用；实现统一下沉到无 JAV 业务归属的公共模块。
    # copy / move 策略拆出去后可以直接改成 import，本类内的这几处别名一并去掉。
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
        only_files: list[str] | None = None,
        managed_download_source: bool = False,
        target_dir_cache: Cloud115TargetDirCache | None = None,
    ) -> ImportJob:
        """执行一次完整的 cloud115 导入，并把中间状态写回 ImportJob。

        ``transfer_mode``: "cleanup-source"（默认，移动源文件进库）或 "copy"；旧 "move" 为别名。
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

        failure_items: list[dict] = []
        stats = {"imported": 0, "skipped": 0, "failed": 0}
        new_playable_movies: dict[int, dict] = {}

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
                    managed_download_source=managed_download_source,
                    target_dir_cache=target_dir_cache,
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
        only_files: list[str] | None,
        failure_items: list[dict],
        stats: dict,
        new_playable_movies: dict[int, dict],
        progress_callback: ImportProgressCallback | None,
        job: ImportJob,
        managed_download_source: bool = False,
        target_dir_cache: Cloud115TargetDirCache | None = None,
    ) -> None:
        config = require_cloud115_library(library)
        root_cid = config["root_cid"]

        def _on_pace_wait(seconds: float) -> None:
            # 批次休息期间不发请求，进度必须显式透出，否则与"卡死"无法区分。
            self._emit(
                progress_callback,
                event="pace_waiting",
                stage="pacing",
                text=f"为降低 115 请求频率，暂停 {seconds:.0f} 秒后继续",
                summary_patch=self._summary(stats, new_playable_movies),
            )

        async with cloud115_client_for(
            library, batch_pacing=True, on_pace_wait=_on_pace_wait
        ) as client:
            if managed_download_source:
                # 自动离线任务只允许读取软件自建的下载缓冲区；一次 dir_info 同时完成
                # 归属校验、名称和展示路径读取，不再重复查询媒体库根目录。
                download_root_cid = str(config.get("download_root_cid") or "")
                if not download_root_cid:
                    raise ValueError("cloud115_download_root_cid_missing")
                source_meta = await client.dir_info(source_cid)
                source_path_ids = {crumb.file_id for crumb in source_meta.paths}
                if (
                    source_cid == download_root_cid
                    or (
                        source_meta.parent_id != download_root_cid
                        and download_root_cid not in source_path_ids
                    )
                ):
                    raise ValueError(
                        "cloud115_managed_source_outside_download_root"
                    )
            else:
                # 手动目录导入保留双向包含校验，并复用校验时已取得的源目录信息。
                source_meta = await assert_cid_outside_library_root(
                    client, source_cid=source_cid, root_cid=root_cid
                )
            # 源目录名参与番号识别（用户常选番号命名的目录本身），并落作业展示路径。
            source_display = "/".join(
                [*(crumb.name for crumb in source_meta.paths), source_meta.name]
            )
            job.source_path = source_display[:1024] or f"cloud115:{source_cid}"
            job.save()

            # 自动批次复用外部缓存；手动作业未传缓存时由 resolver 创建仅本作业有效的缓存。
            target_dir_resolver = Cloud115TargetDirResolver(
                client,
                root_cid=root_cid,
                cache=target_dir_cache,
            )

            # 1) 枚举 + 分拣 + 去重
            groups, scan_skipped, scan_failed = await scan_cloud115_source(
                client,
                library=library,
                source_cid=source_cid,
                source_name=source_meta.name,
                transfer_mode=transfer_mode,
                only_files=only_files,
                failure_items=failure_items,
                progress_callback=progress_callback,
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
                await self._cleanup_managed_source_dir(
                    client,
                    config=config,
                    source_cid=source_cid,
                    managed_download_source=managed_download_source,
                    failure_items=failure_items,
                    stats=stats,
                )
                return

            # 2) 元数据并发抓取 + 逐番号搬运登记（目标目录/版本目录按番号在 _import_group 里建）
            with self._media_import_service.metadata_import_batch(
                [group.movie_number for group in groups],
                thread_name_prefix="cloud115-import-metadata",
            ) as metadata_futures:
                for group_index, group in enumerate(groups):
                    movie_number = group.movie_number
                    if not managed_download_source and group_index > 0:
                        delay = random.uniform(
                            MANUAL_GROUP_REST_MIN_SECONDS,
                            MANUAL_GROUP_REST_MAX_SECONDS,
                        )
                        logger.info(
                            "Cloud115 manual import resting before next movie "
                            "job_id={} movie_number={} delay_seconds={:.1f}",
                            job.id,
                            movie_number,
                            delay,
                        )
                        self._emit(
                            progress_callback,
                            event="movie_waiting",
                            stage="rest",
                            movie_number=movie_number,
                            completed_movies=completed_movies,
                            total_movies=total_movies,
                            current=completed_movies,
                            total=total_movies,
                            text=f"为降低 115 请求频率，{delay:.1f} 秒后继续导入 {movie_number}",
                            summary_patch=self._summary(stats, new_playable_movies),
                        )
                        await asyncio.sleep(delay)

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
                        target_dir_resolver=target_dir_resolver,
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

            # 3) 全部搬运完成后清理离线缓冲区里的来源任务目录。
            await self._cleanup_managed_source_dir(
                client,
                config=config,
                source_cid=source_cid,
                managed_download_source=managed_download_source,
                failure_items=failure_items,
                stats=stats,
            )

    # ---- 来源目录清理 ----

    @staticmethod
    async def _cleanup_managed_source_dir(
        client: Cloud115Client,
        *,
        config: dict,
        source_cid: str,
        managed_download_source: bool,
        failure_items: list[dict],
        stats: dict,
    ) -> None:
        """自动离线导入完成后，把来源任务目录整个删掉（进 115 回收站）。

        cleanup-source 走 move，只搬文件不动目录，已导入完成的任务目录会永久残留在
        ``sakuramedia_downloads`` 下；下次按整个缓冲区导入时，扫描要把这些空壳逐个列一遍，
        成本随历史下载数无限增长（实测 158+ 个残留目录 → 连续 200 余次 list_dir → WAF 405）。

        三重前提缺一不可：
        1. 仅限软件自建的离线缓冲区来源，用户手动选的目录一律不动；
        2. 本次无失败项——番号识别不出的视频计入 failed，因此不会误删还需重导的内容；
        3. 来源不是缓冲区根目录本身（防御性兜底，执行阶段已校验过归属）。

        非视频残留（nfo / 封面 / 种子 / 判定过小的样本）一并删除，回收站提供误删缓冲。
        删除失败只记告警：文件已入库，不该把作业翻成失败。
        """
        if not managed_download_source or stats["failed"] > 0:
            return
        download_root_cid = str(config.get("download_root_cid") or "")
        if not download_root_cid or source_cid == download_root_cid:
            return
        try:
            await client.delete_files([source_cid])
        except Exception as exc:
            item = make_failure_item(
                f"cloud115:{source_cid}",
                FAILURE_REASON_SOURCE_DELETE_FAILED,
                f"来源任务目录清理失败: {exc}",
            )
            item["kind"] = FAILED_FILE_KIND_WARNING
            failure_items.append(item)
            logger.warning(
                "Cloud115 managed source dir cleanup failed source_cid={} detail={}",
                source_cid, exc,
            )
            return
        logger.info(
            "Cloud115 managed source dir cleaned up source_cid={}", source_cid
        )

    # ---- 搬运 / 对账 / 登记 ----

    async def _import_group(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        movie: Movie,
        group: CloudImportGroup,
        target_dir_resolver: Cloud115TargetDirResolver,
        transfer_mode: str,
        failure_items: list[dict],
        stats: dict,
        new_playable_movies: dict[int, dict],
    ) -> None:
        """按模式分派：cleanup-source 真正移动源文件，copy 复制并保留源。"""
        handler = (
            self._import_group_by_move
            if transfer_mode == CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
            else self._import_group_by_copy
        )
        await handler(
            client,
            library=library,
            movie=movie,
            group=group,
            target_dir_resolver=target_dir_resolver,
            failure_items=failure_items,
            stats=stats,
            new_playable_movies=new_playable_movies,
        )

    async def _import_group_by_copy(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        movie: Movie,
        group: CloudImportGroup,
        target_dir_resolver: Cloud115TargetDirResolver,
        failure_items: list[dict],
        stats: dict,
        new_playable_movies: dict[int, dict],
    ) -> None:
        """复制并登记一个番号组；任一改名失败时整组不入库。源文件始终保留。

        目标结构对齐本地：``jav/{番号}/{版本ms}/{番号}{ext}``。同番号多分部共享
        实体目录，各自版本子目录；中断恢复时按番号目录做 sha1 对账，命中即复用远端
        已复制条目，不重复复制。
        """
        try:
            entity_cid, entity_created = (
                await target_dir_resolver.resolve_jav_entity(group.movie_number)
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
            entity_entries_by_sha1 = (
                {}
                if entity_created
                else await list_cloud115_entity_target_files(client, entity_cid)
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
        resolved: list[_ResolvedFile] = []
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
                    version_cid = await target_dir_resolver.create_version_dir(
                        entity_cid=entity_cid,
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
                        "Cloud115 copy failed movie_number={} detail={}",
                        group.movie_number, exc,
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
        probe_results: dict[str, MediaMetadataProbeResult | None] = {}
        for r in resolved:
            existing_valid = Cloud115MediaRegistrar.find_library_media(library, r.cloud_file.sha1, valid=True)
            if r.cloud_file.censored or (
                existing_valid is not None and existing_valid.video_info is not None
            ):
                probe_results[r.cloud_file.sha1] = None
                continue
            try:
                probe_results[r.cloud_file.sha1] = await probe_cloud115_media(client, self._media_metadata_probe_service,
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
        registration_results: list[tuple[CloudSourceFile, bool]] = []
        try:
            with get_database().atomic():
                for r in resolved:
                    _media, registered = register_media(
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

        # 6) 字幕下载到本地；copy 模式源文件始终保留，字幕失败只作告警。
        for r in resolved:
            if r.cloud_file.subtitle is None:
                continue
            try:
                await import_subtitle(
                    client,
                    movie=movie,
                    cloud_file=r.cloud_file,
                )
            except Exception as exc:
                failure_items.append(
                    make_failure_item(
                        r.cloud_file.rel_path,
                        FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                        str(exc),
                    )
                )
                logger.warning(
                    "Cloud115 subtitle download failed movie_number={} rel_path={} detail={}",
                    group.movie_number, r.cloud_file.rel_path, exc,
                )

    async def _import_group_by_move(
        self,
        client: Cloud115Client,
        *,
        library: MediaLibrary,
        movie: Movie,
        group: CloudImportGroup,
        target_dir_resolver: Cloud115TargetDirResolver,
        failure_items: list[dict],
        stats: dict,
        new_playable_movies: dict[int, dict],
    ) -> None:
        """把源文件真正移动进 ``jav/{番号}/{版本ms}/{番号}{ext}``，不复制也不删源。

        115 的 move 保持 fid / pickcode 不变，而 cloud115 Media 只靠 pickcode 定位、
        与文件所在目录无关，所以登记可以**先于**搬运完成。顺序固定为：
        探测(源 pickcode) → 字幕 → 建版本目录 → 登记 Media → move → rename。
        依赖源文件的步骤全部前置，于是任一步失败时只有两种局面：源原地未动（可完整
        重导），或文件已在受管目录且 Media 有效（可播）。不会出现"文件已搬进库、
        却没有任何记录"的孤儿——移动语义下源已消失，那种孤儿是无法自动收回的。
        """
        # 1) 按库内已有记录分流。fid 相同说明登记的就是这个源文件本身（上一轮登记
        # 成功但搬运没走完），必须继续搬运；只有 fid 不同才是真正多余的副本，删源。
        pending: list[CloudSourceFile] = []
        # 续做搬运的文件：Media 不是新建的，但本轮确实把它搬进了库，统计上算导入成功，
        # 否则用户重导后会看到"跳过"，与他刚刚完成的重试动作对不上。
        resuming_fids: set[str] = set()
        for cloud_file in group.files:
            existing_valid = Cloud115MediaRegistrar.find_library_media(
                library, cloud_file.sha1, valid=True
            )
            if existing_valid is None:
                pending.append(cloud_file)
                continue
            registered_fid = str(
                (existing_valid.backend_locator or {}).get("fid") or ""
            )
            if registered_fid == cloud_file.fid:
                pending.append(cloud_file)
                resuming_fids.add(cloud_file.fid)
                continue
            stats["skipped"] += 1
            failure_items.append(
                make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_DUPLICATE_FINGERPRINT,
                    f"库中已存在相同内容（media_id={existing_valid.id}）",
                )
            )
            await self._cleanup_duplicate_source(
                client,
                movie=movie,
                cloud_file=cloud_file,
                failure_items=failure_items,
                stats=stats,
            )

        if not pending:
            return

        # 2) 探测用源 pickcode：move 不改 pickcode，与搬运后探测等价，但失败时源未动。
        probe_results: dict[str, MediaMetadataProbeResult | None] = {}
        for cloud_file in pending:
            existing_valid = Cloud115MediaRegistrar.find_library_media(
                library, cloud_file.sha1, valid=True
            )
            if cloud_file.censored or (
                existing_valid is not None and existing_valid.video_info is not None
            ):
                probe_results[cloud_file.sha1] = None
                continue
            try:
                probe_results[cloud_file.sha1] = await probe_cloud115_media(client, self._media_metadata_probe_service,
                    pickcode=cloud_file.pickcode,
                    file_size_bytes=cloud_file.size,
                )
            except Exception as exc:
                record_files_failure(
                    pending,
                    reason=FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED,
                    detail=f"{cloud_file.rel_path}: {exc}",
                    failure_items=failure_items,
                    stats=stats,
                )
                logger.warning(
                    "Cloud115 metadata probe failed movie_number={} rel_path={} detail={}",
                    group.movie_number, cloud_file.rel_path, exc,
                )
                return

        # 3) 字幕同样前置：源一旦移走就无法重导，字幕失败必须在源完好时终止整组。
        for cloud_file in pending:
            if cloud_file.subtitle is None:
                continue
            try:
                await import_subtitle(
                    client,
                    movie=movie,
                    cloud_file=cloud_file,
                )
            except Exception as exc:
                record_files_failure(
                    pending,
                    reason=FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                    detail=f"{cloud_file.rel_path}: {exc}",
                    failure_items=failure_items,
                    stats=stats,
                    kind=FAILED_FILE_KIND_FILE,
                )
                logger.warning(
                    "Cloud115 subtitle download failed movie_number={} rel_path={} detail={}",
                    group.movie_number, cloud_file.rel_path, exc,
                )
                return

        # 4) 解析番号目录并为每个文件建独立版本目录。移动模式不需要读目标目录对账：
        # 幂等靠"源还在不在"判断，源已移走的文件本轮根本扫不到。
        try:
            entity_cid, _ = await target_dir_resolver.resolve_jav_entity(
                group.movie_number
            )
        except Exception as exc:
            record_files_failure(
                pending,
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

        version_cids: dict[str, str] = {}
        try:
            for cloud_file in pending:
                version_cids[cloud_file.fid] = (
                    await target_dir_resolver.create_version_dir(
                        entity_cid=entity_cid,
                        now_ms=int(time.time() * 1000),
                    )
                )
        except Exception as exc:
            # 新建的版本目录本轮独占，回收掉避免在番号目录下累积空目录；
            # entity_cid 可能被同番号其它导入共享，不动。
            await self._discard_version_dirs(
                client, version_cids.values(), movie_number=group.movie_number
            )
            record_files_failure(
                pending,
                reason=FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                detail=f"创建版本目录失败: {exc}",
                failure_items=failure_items,
                stats=stats,
            )
            logger.exception(
                "Cloud115 version dir failed movie_number={} detail={}",
                group.movie_number, exc,
            )
            return

        # 5) 整组 Media 在同一事务登记，locator 直接用源 fid/pickcode（move 不改这两个值）。
        registered: list[tuple[CloudSourceFile, Media, bool]] = []
        try:
            with get_database().atomic():
                for cloud_file in pending:
                    media, is_new = register_media(
                        library=library,
                        movie=movie,
                        cloud_file=cloud_file,
                        target_fid=cloud_file.fid,
                        target_pickcode=cloud_file.pickcode,
                        encoded_name=normalize_jav_media_filename(
                            group.movie_number, cloud_file.name
                        ),
                        metadata=probe_results[cloud_file.sha1],
                    )
                    registered.append((cloud_file, media, is_new))
        except Exception as exc:
            await self._discard_version_dirs(
                client, version_cids.values(), movie_number=group.movie_number
            )
            record_files_failure(
                pending,
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

        # 6) 搬运。失败时源原地未动、Media 已登记且可播，失败项可重导——重导会命中
        # 步骤 1 的 fid 相同分支，补完搬运即收敛。
        moved: list[tuple[CloudSourceFile, Media, bool]] = []
        for cloud_file, media, is_new in registered:
            try:
                await client.move_files(
                    [cloud_file.fid], pid=version_cids[cloud_file.fid]
                )
            except Exception as exc:
                await self._discard_version_dirs(
                    client,
                    [version_cids[cloud_file.fid]],
                    movie_number=group.movie_number,
                )
                stats["failed"] += 1
                failure_items.append(
                    make_failure_item(
                        cloud_file.rel_path,
                        FAILURE_REASON_CLOUD115_TRANSFER_FAILED,
                        str(exc),
                    )
                )
                logger.warning(
                    "Cloud115 move failed movie_number={} rel_path={} detail={}",
                    group.movie_number, cloud_file.rel_path, exc,
                )
                continue
            moved.append((cloud_file, media, is_new))

        # 7) 改名 + 校验，并结算已搬运文件的统计。
        for cloud_file, media, is_new in moved:
            target_name = normalize_jav_media_filename(
                group.movie_number, cloud_file.name
            )
            if cloud_file.name != target_name:
                try:
                    await client.rename_file(cloud_file.fid, target_name)
                    await self._verify_renamed_file(
                        client, cloud_file.fid, target_name
                    )
                except Exception as exc:
                    # 文件已在受管目录、Media 可播，源已不存在，重导无从下手：
                    # 把 locator.name 回写成 115 上的实际名保持一致，并降为告警项。
                    self._restore_locator_name(media, cloud_file.name)
                    item = make_failure_item(
                        cloud_file.rel_path,
                        FAILURE_REASON_CLOUD115_RENAME_FAILED,
                        str(exc),
                    )
                    item["kind"] = FAILED_FILE_KIND_WARNING
                    failure_items.append(item)
                    logger.warning(
                        "Cloud115 rename verification failed movie_number={} fid={} detail={}",
                        group.movie_number, cloud_file.fid, exc,
                    )
            if cloud_file.censored:
                # 违规文件按 invalid 登记完成，报告但不计入可播放。
                stats["imported"] += 1
                failure_items.append(
                    make_failure_item(
                        cloud_file.rel_path,
                        FAILURE_REASON_CLOUD115_FILE_CENSORED,
                        "115 标记该文件违规（ic=1）",
                    )
                )
            elif is_new or cloud_file.fid in resuming_fids:
                stats["imported"] += 1
                new_playable_movies[movie.id] = {
                    "movie_id": movie.id,
                    "movie_number": movie.movie_number,
                    "title": movie.title,
                }
            else:
                stats["skipped"] += 1

        # 8) 视频本身已经移走，只剩配对字幕的远端源要清理（本地副本已落盘）。
        # 清不掉只残留一个 .srt，且源视频已不在、无法通过重导再试，只作告警。
        for cloud_file, _media, _is_new in moved:
            if cloud_file.subtitle is None:
                continue
            try:
                await client.delete_files([cloud_file.subtitle.fid])
            except Exception as exc:
                failure_items.append(
                    make_failure_item(
                        cloud_file.rel_path,
                        FAILURE_REASON_SOURCE_DELETE_FAILED,
                        f"源字幕删除失败: {exc}",
                    )
                )
                logger.warning(
                    "Cloud115 source subtitle delete failed movie_number={} rel_path={} detail={}",
                    group.movie_number, cloud_file.rel_path, exc,
                )

    async def _cleanup_duplicate_source(
        self,
        client: Cloud115Client,
        *,
        movie: Movie,
        cloud_file: CloudSourceFile,
        failure_items: list[dict],
        stats: dict,
    ) -> None:
        """库内已有该内容的独立副本时，只清掉这一份多余的源（含配对字幕）。

        字幕先下载到本地再删远端；下载失败就整份保留不删，让用户可以重导。
        """
        if cloud_file.subtitle is not None:
            try:
                await import_subtitle(
                    client,
                    movie=movie,
                    cloud_file=cloud_file,
                )
            except Exception as exc:
                stats["failed"] += 1
                item = make_failure_item(
                    cloud_file.rel_path,
                    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED,
                    str(exc),
                )
                item["kind"] = FAILED_FILE_KIND_FILE
                failure_items.append(item)
                logger.warning(
                    "Cloud115 duplicate source subtitle failed rel_path={} detail={}",
                    cloud_file.rel_path, exc,
                )
                return

        fids = [cloud_file.fid]
        if cloud_file.subtitle is not None:
            fids.append(cloud_file.subtitle.fid)
        try:
            await client.delete_files(fids)
        except Exception as exc:
            stats["failed"] += 1
            # 源还在，重导可以再试一次删除。
            item = make_failure_item(
                cloud_file.rel_path, FAILURE_REASON_SOURCE_DELETE_FAILED, str(exc)
            )
            item["kind"] = FAILED_FILE_KIND_FILE
            failure_items.append(item)
            logger.warning(
                "Cloud115 duplicate source delete failed rel_path={} detail={}",
                cloud_file.rel_path, exc,
            )

    @staticmethod
    async def _discard_version_dirs(
        client: Cloud115Client,
        version_cids,
        *,
        movie_number: str,
    ) -> None:
        """回收本轮新建、最终没用上的空版本目录（走回收站）；失败仅告警。"""
        for version_cid in version_cids:
            try:
                await client.delete_files([version_cid])
            except Exception as exc:
                logger.warning(
                    "Cloud115 version dir discard failed movie_number={} version_cid={} detail={}",
                    movie_number, version_cid, exc,
                )

    @staticmethod
    def _restore_locator_name(media: Media, actual_name: str) -> None:
        """改名失败时把 locator.name 回写为 115 上的实际名，保持记录与远端一致。"""
        locator = dict(media.backend_locator or {})
        if locator.get("name") == actual_name:
            return
        locator["name"] = actual_name
        media.backend_locator = locator
        media.updated_at = utc_now_for_db()
        media.save()

    @classmethod
    def _record_group_failure(
        cls,
        group: CloudImportGroup,
        *,
        reason: str,
        detail: str,
        failure_items: list[dict],
        stats: dict,
    ) -> None:
        record_files_failure(
            group.files,
            reason=reason,
            detail=detail,
            failure_items=failure_items,
            stats=stats,
        )

    @staticmethod
    def _resolve_registered_entry(candidates: list[DirEntry], media: Media) -> DirEntry | None:
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

    # ---- 进度 ----

    @staticmethod
    def _summary(stats: dict, new_playable_movies: dict[int, dict]) -> dict:
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
