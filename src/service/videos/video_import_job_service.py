"""非 JAV 视频导入作业 service。

负责视频目录导入的异步触发与查询。后台执行复用 ``DownloadImportRunner`` 线程池与
``ActivityService`` 任务运行链路，触发防重依赖 ``BackgroundTaskRun.mutex_key`` 唯一约束，
与 JAV 目录导入（``MediaImportJobService``）保持一致的形态。
"""

import hashlib
import json
from pathlib import Path
from typing import Any, List

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.fs_browse import assert_within_allowed_roots, normalize_abs_path
from src.common.media_import_status import (
    FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED,
    FAILURE_REASON_IMPORT_JOB_INTERRUPTED,
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    classify_failed_file_kind,
    make_failure_item,
)
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import MediaLibrary, VideoCollection, VideoImportJob
from src.schema.transfers.media_import import FailedFileResource
from src.schema.videos.imports import VideoImportJobResource, VideoImportTriggerResponse
from src.service.system import ActivityService
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.service.videos.video_import_service import VideoImportService

TASK_KEY = "video_directory_import"


class VideoImportJobService:
    @classmethod
    def trigger_directory_import(
        cls,
        library_id: int,
        source_path: str,
        *,
        transfer_mode: str = "auto",
        collection_id: int | None = None,
    ) -> VideoImportTriggerResponse:
        if transfer_mode not in ("auto", "cleanup-source"):
            raise ApiError(422, "invalid_transfer_mode", "无效的导入模式", {"transfer_mode": transfer_mode})

        library = cls._require_library(library_id)
        cls._validate_collection(collection_id)
        resolved_source = cls._resolve_source_path(source_path)
        # cleanup-source 会删除源文件，触发时即拒绝指向媒体库目录内的源，给前端即时反馈（不必等后台作业失败）。
        if transfer_mode == "cleanup-source":
            VideoImportService._assert_source_outside_libraries(resolved_source)
        mutex_key = cls._build_mutex_key(library_id, resolved_source)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            transfer_mode=transfer_mode,
            collection_id=collection_id,
            mutex_key=mutex_key,
            task_name=f"视频导入 {resolved_source.name or resolved_source}",
        )

    @classmethod
    def get_job(cls, video_import_job_id: int) -> VideoImportJobResource:
        job = cls._require_job(video_import_job_id)
        return VideoImportJobResource.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def recover_orphaned_jobs(cls) -> dict:
        """回收中断的视频导入作业。

        把没有存活 owner 进程、也没有活跃后台线程的 pending/running 作业复位为 failed，
        并补一条任务级失败记录；对应 task_run 一并回收。
        """
        recovered_count = 0
        orphaned_jobs = (
            VideoImportJob.select()
            .where(VideoImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
            .order_by(VideoImportJob.id.asc())
        )
        for job in orphaned_jobs:
            if cls._has_live_owner(job) or DownloadImportRunner.has_active_job(job.id):
                continue
            failure_items = cls._parse_failed_files(job)
            failure_items.append(
                make_failure_item(job.source_path, FAILURE_REASON_IMPORT_JOB_INTERRUPTED, "视频导入进程已中断，作业未完成")
            )
            job.failed_count = max(job.failed_count, 1)
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.state = IMPORT_JOB_STATE_FAILED
            job.finished_at = utc_now_for_db()
            job.updated_at = utc_now_for_db()
            job.save()
            if job.task_run_id is not None:
                ActivityService.recover_task_run(
                    job.task_run_id,
                    error_message="视频导入进程已中断，作业已失败",
                    result_summary={"video_import_job_id": job.id},
                    allow_null_owner=True,
                )
            recovered_count += 1
            logger.warning(
                "Recovered orphaned video import job_id={} source_path={}",
                job.id,
                job.source_path,
            )
        return {"recovered_count": recovered_count}

    # ---- 内部触发与执行 ----

    @classmethod
    def _launch_import(
        cls,
        *,
        library: MediaLibrary,
        resolved_source: Path,
        transfer_mode: str,
        collection_id: int | None,
        mutex_key: str,
        task_name: str,
    ) -> VideoImportTriggerResponse:
        try:
            task_run = ActivityService.create_task_run(
                task_key=TASK_KEY,
                task_name=task_name,
                trigger_type="manual",
                mutex_key=mutex_key,
            )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "video_import_conflict",
                "相同导入源已有进行中的任务",
                {
                    "mutex_key": mutex_key,
                    "blocking_task_run_id": blocking.id if blocking is not None else None,
                },
            ) from exc

        # task_run 建好后到入队成功之间任一步骤失败都必须回收 task_run（释放 mutex_key），
        # 否则该 mutex_key 会长期占用导致同库同源永久 409。
        import_job: VideoImportJob | None = None
        try:
            import_job = VideoImportJob.create(
                source_path=str(resolved_source),
                library=library,
                collection=collection_id,
                state=IMPORT_JOB_STATE_PENDING,
                transfer_mode=transfer_mode,
            )
            import_job.task_run = task_run
            import_job.save()
            DownloadImportRunner.submit(
                import_job.id,
                cls._run_import_job,
                library.id,
                str(resolved_source),
                import_job.id,
                task_run.id,
                transfer_mode,
                collection_id,
            )
        except Exception as exc:
            import_job_id = import_job.id if import_job is not None else None
            if import_job is not None:
                failure_items = cls._parse_failed_files(import_job)
                failure_items.append(
                    make_failure_item(resolved_source, FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED, str(exc))
                )
                import_job.failed_count = max(import_job.failed_count, 1)
                import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
                import_job.state = IMPORT_JOB_STATE_FAILED
                import_job.finished_at = utc_now_for_db()
                import_job.save()
            ActivityService.fail_task_run(
                task_run.id,
                error_message=str(exc),
                result_summary={"video_import_job_id": import_job_id},
            )
            raise ApiError(
                502,
                "video_import_failed",
                "视频导入任务入队失败",
                {"detail": str(exc), "video_import_job_id": import_job_id},
            ) from exc

        return VideoImportTriggerResponse(
            video_import_job_id=import_job.id,
            task_run_id=task_run.id,
            status="accepted",
        )

    @classmethod
    def _run_import_job(
        cls,
        library_id: int,
        source_path: str,
        video_import_job_id: int,
        task_run_id: int,
        transfer_mode: str,
        collection_id: int | None,
    ) -> dict:
        ensure_database_ready()
        try:
            def _run_task(reporter):
                service = VideoImportService()
                job = service.import_from_source(
                    source_path,
                    library_id,
                    video_import_job_id=video_import_job_id,
                    transfer_mode=transfer_mode,
                    collection_id=collection_id,
                    progress_callback=reporter.progress_callback,
                )
                return {
                    "video_import_job_id": job.id,
                    "imported_count": job.imported_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "job_state": job.state,
                }

            return ActivityService.run_task(
                task_key=TASK_KEY,
                task_name=None,
                trigger_type="internal",
                task_run_id=task_run_id,
                func=_run_task,
            )
        except Exception as exc:
            cls._mark_import_failed(video_import_job_id, str(exc))
            logger.exception(
                "Video directory import failed video_import_job_id={} source_path={}",
                video_import_job_id,
                source_path,
            )
            return {
                "video_import_job_id": video_import_job_id,
                "job_state": IMPORT_JOB_STATE_FAILED,
            }

    @staticmethod
    def _mark_import_failed(video_import_job_id: int, detail: str) -> None:
        import_job = VideoImportJob.get_or_none(VideoImportJob.id == video_import_job_id)
        if import_job is None:
            return
        # import_from_source 崩溃时已自行写好终态与失败明细，这里只兜底引导/setup 阶段的失败，
        # 已是终态的作业直接跳过，避免重复追加一条 job 级失败记录。
        if import_job.state in (IMPORT_JOB_STATE_FAILED, IMPORT_JOB_STATE_COMPLETED):
            return
        failure_items: list[dict[str, Any]] = []
        try:
            if import_job.failed_files:
                failure_items = json.loads(import_job.failed_files)
        except json.JSONDecodeError:
            failure_items = []
        failure_items.append(
            make_failure_item(import_job.source_path, FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED, detail)
        )
        import_job.failed_count = max(import_job.failed_count, 1)
        import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        import_job.state = IMPORT_JOB_STATE_FAILED
        import_job.finished_at = utc_now_for_db()
        import_job.save()

    # ---- 校验与失败文件解析 ----

    @staticmethod
    def _require_library(library_id: int) -> MediaLibrary:
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在", {"library_id": library_id})
        return library

    @staticmethod
    def _validate_collection(collection_id: int | None) -> None:
        if collection_id is None:
            return
        if VideoCollection.get_or_none(VideoCollection.id == collection_id) is None:
            raise ApiError(404, "video_collection_not_found", "视频合集不存在", {"collection_id": collection_id})

    @staticmethod
    def _require_job(video_import_job_id: int) -> VideoImportJob:
        job = VideoImportJob.get_or_none(VideoImportJob.id == video_import_job_id)
        if job is None:
            raise ApiError(404, "video_import_job_not_found", "视频导入作业不存在", {"video_import_job_id": video_import_job_id})
        return job

    @staticmethod
    def _has_live_owner(job: VideoImportJob) -> bool:
        task_run = job.task_run
        if task_run is None or task_run.owner_pid is None:
            return False
        return ActivityService._is_process_alive(task_run.owner_pid)

    @staticmethod
    def _resolve_source_path(source_path: str) -> Path:
        resolved = normalize_abs_path(source_path)
        assert_within_allowed_roots(resolved, settings.media_import.browse_roots)
        return resolved

    @staticmethod
    def _build_mutex_key(library_id: int, resolved_source: Path) -> str:
        digest = hashlib.sha1(str(resolved_source).encode("utf-8")).hexdigest()
        return f"video_import:{library_id}:{digest}"

    @staticmethod
    def _parse_failed_files(job: VideoImportJob) -> list[dict[str, Any]]:
        if not job.failed_files:
            return []
        try:
            items = json.loads(job.failed_files)
        except json.JSONDecodeError:
            return []
        return items if isinstance(items, list) else []

    @classmethod
    def _failed_file_resources(cls, job: VideoImportJob) -> list[FailedFileResource]:
        resources = []
        for item in cls._parse_failed_files(job):
            if not isinstance(item, dict):
                continue
            kind = item.get("kind") or classify_failed_file_kind(item.get("reason", ""))
            resources.append(
                FailedFileResource(
                    path=item.get("path", ""),
                    reason=item.get("reason", ""),
                    detail=item.get("detail", ""),
                    kind=kind,
                )
            )
        return resources
