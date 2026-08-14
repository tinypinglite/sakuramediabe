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
from collections.abc import Callable

from loguru import logger

from src.common.media_import_status import (
    FAILED_FILE_KIND_WARNING,
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
from src.common.service_helpers import emit_progress, rest_between_requests_async
from src.lib.cloud115 import Cloud115Client
from src.model import ImportJob, MediaLibrary, Movie
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    require_cloud115_library,
)
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.cloud115.importer.common import (
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    Cloud115TargetDirCache,
    Cloud115TargetDirResolver,
    normalize_cloud115_transfer_mode,
)
from src.service.transfers.cloud115.importer.scanner import scan_cloud115_source
from src.service.transfers.cloud115.importer.strategies.copy import import_group_by_copy
from src.service.transfers.cloud115.importer.strategies.move import import_group_by_move
from src.service.transfers.cloud115.importer.types import CloudImportGroup
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
            emit_progress(
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
            emit_progress(
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
            emit_progress(
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
            emit_progress(
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
                        # 先取延迟并报"等待中"事件，再真正休息——事件语义是"即将休息到 delay 秒后"。
                        delay = await rest_between_requests_async(
                            MANUAL_GROUP_REST_MIN_SECONDS,
                            MANUAL_GROUP_REST_MAX_SECONDS,
                        )
                        emit_progress(
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
                        logger.info(
                            "Cloud115 manual import resting before next movie "
                            "job_id={} movie_number={} delay_seconds={:.1f}",
                            job.id,
                            movie_number,
                            delay,
                        )
                        await asyncio.sleep(delay)
                    emit_progress(
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
                        emit_progress(
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
                    emit_progress(
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
                    emit_progress(
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
        2. 本次无失败项；有媒体产出时可以清理，番号识别不出的视频计入 failed，
           零产出但有 skipped 的候选必须保留，便于查看文件和重导；
        3. 来源不是缓冲区根目录本身（防御性兜底，执行阶段已校验过归属）。

        非视频残留（nfo / 封面 / 种子 / 判定过小的样本）一并删除，回收站提供误删缓冲。
        删除失败只记告警：文件已入库，不该把作业翻成失败。
        """
        if (
            not managed_download_source
            or stats["failed"] > 0
            or (stats["imported"] == 0 and stats["skipped"] > 0)
        ):
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
        strategy = (
            import_group_by_move
            if transfer_mode == CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
            else import_group_by_copy
        )
        await strategy(
            client,
            library=library,
            movie=movie,
            group=group,
            target_dir_resolver=target_dir_resolver,
            failure_items=failure_items,
            stats=stats,
            new_playable_movies=new_playable_movies,
            probe_service=self._media_metadata_probe_service,
        )

    # ---- 进度 ----

    @staticmethod
    def _summary(stats: dict, new_playable_movies: dict[int, dict]) -> dict:
        return {
            "imported_count": stats["imported"],
            "skipped_count": stats["skipped"],
            "failed_count": stats["failed"],
            "new_playable_movies": list(new_playable_movies.values()),
        }
