"""非 JAV 视频导入作业 service。

负责视频目录导入的异步触发与查询、失败文件的删除/重命名/重导。通用作业生命周期下沉到
``BaseImportJobService``，这里只保留 videos 专属的归属模型、合集透传、错误码文案、后台执行入口
与孤儿筛选。后台执行复用 ``DownloadImportRunner`` 线程池与 ``ActivityService`` 任务运行链路，
触发防重依赖 ``BackgroundTaskRun.mutex_key`` 唯一约束，与 JAV 目录导入保持一致的形态。
"""

from typing import List

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
)
from src.model import VideoCollection, VideoImportJob
from src.model.enums import MediaLibraryBackend
from src.schema.videos.imports import (
    VideoImportJobListItemResource,
    VideoImportJobResource,
    VideoImportTriggerResponse,
)
from src.service.system import ActivityService
from src.service.transfers.base_import_job_service import BaseImportJobService
from src.service.videos.video_import_service import VideoImportService


class VideoImportJobService(BaseImportJobService):
    REQUIRED_LIBRARY_BACKEND = MediaLibraryBackend.LOCAL
    JOB_MODEL = VideoImportJob
    TASK_KEY = "video_directory_import"
    MUTEX_PREFIX = "video_import"
    LIST_RESOURCE = VideoImportJobListItemResource
    DETAIL_RESOURCE = VideoImportJobResource
    TRIGGER_RESPONSE = VideoImportTriggerResponse
    JOB_ID_FIELD = "video_import_job_id"
    JOB_NOT_FOUND_CODE = "video_import_job_not_found"
    JOB_NOT_FOUND_MESSAGE = "视频导入作业不存在"
    CONFLICT_CODE = "video_import_conflict"
    LAUNCH_FAILED_CODE = "video_import_failed"
    LAUNCH_FAILED_MESSAGE = "视频导入任务入队失败"
    TRIGGER_TASK_NAME_PREFIX = "视频导入"
    RETRY_TASK_NAME_PREFIX = "重导失败视频 #"
    INTERRUPTED_FAILURE_DETAIL = "视频导入进程已中断，作业未完成"
    INTERRUPTED_RECOVER_MESSAGE = "视频导入进程已中断，作业已失败"
    LOG_LABEL = "Video import"
    RECOVER_LOG_LABEL = "Recovered orphaned video import"

    @classmethod
    def trigger_directory_import(
        cls,
        library_id: int,
        source_path: str,
        *,
        transfer_mode: str = "auto",
        collection_id: int | None = None,
    ) -> VideoImportTriggerResponse:
        return cls._do_trigger(
            library_id,
            source_path,
            transfer_mode=transfer_mode,
            collection_id=collection_id,
        )

    # ---- videos 专属钩子 ----

    @classmethod
    def _pre_launch_validate(cls, *, collection_id: int | None = None, **trigger_kwargs) -> None:
        cls._validate_collection(collection_id)

    @classmethod
    def _assert_transfer_mode_constraints(
        cls, resolved_source, transfer_mode, *, collection_id: int | None = None, **trigger_kwargs
    ) -> None:
        # cleanup-source 会删除源文件，触发时即拒绝指向媒体库目录内的源，给前端即时反馈（不必等后台作业失败）。
        if transfer_mode == "cleanup-source":
            VideoImportService._assert_source_outside_libraries(resolved_source)

    @classmethod
    def _launch_kwargs_from_trigger(cls, *, collection_id: int | None = None, **trigger_kwargs) -> dict:
        return {"collection_id": collection_id}

    @classmethod
    def _launch_kwargs_from_retry(cls, job) -> dict:
        # 重导沿用原作业的合集归属，避免丢失。
        return {"collection_id": job.collection_id}

    @staticmethod
    def _validate_collection(collection_id: int | None) -> None:
        if collection_id is None:
            return
        if VideoCollection.get_or_none(VideoCollection.id == collection_id) is None:
            raise ApiError(404, "video_collection_not_found", "视频合集不存在", {"collection_id": collection_id})

    @classmethod
    def _create_job(cls, *, library, resolved_source, transfer_mode, collection_id: int | None = None, **launch_kwargs):
        return VideoImportJob.create(
            source_path=str(resolved_source),
            library=library,
            collection=collection_id,
            state=IMPORT_JOB_STATE_PENDING,
            transfer_mode=transfer_mode,
        )

    @classmethod
    def execute_from_queue(cls, reporter, params: dict) -> dict:
        job = cls._require_job(int(params["video_import_job_id"]))
        try:
            service = VideoImportService()
            result_job = service.import_from_source(
                job.source_path,
                job.library_id,
                video_import_job_id=job.id,
                transfer_mode=job.transfer_mode or "auto",
                collection_id=job.collection_id,
                only_files=params.get("only_files"),
                progress_callback=reporter.progress_callback,
            )
        except Exception as exc:
            cls._mark_import_failed(job.id, str(exc))
            logger.exception(
                "Video directory import failed video_import_job_id={} source_path={}",
                job.id,
                job.source_path,
            )
            raise
        return {
            "video_import_job_id": result_job.id,
            "imported_count": result_job.imported_count,
            "skipped_count": result_job.skipped_count,
            "failed_count": result_job.failed_count,
            "job_state": result_job.state,
        }

    @classmethod
    def _orphan_jobs_query(cls):
        return (
            VideoImportJob.select()
            .where(VideoImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
            .order_by(VideoImportJob.id.asc())
        )


def video_import_job_service_for(job_id: int):
    job = VideoImportJob.get_or_none(VideoImportJob.id == job_id)
    if job is None:
        raise ApiError(
            404,
            VideoImportJobService.JOB_NOT_FOUND_CODE,
            VideoImportJobService.JOB_NOT_FOUND_MESSAGE,
            {VideoImportJobService.JOB_ID_FIELD: job_id},
        )
    if job.source_cid or job.source_fid:
        from src.service.videos.cloud115_video_import_job_service import (
            Cloud115VideoImportJobService,
        )

        return Cloud115VideoImportJobService
    return VideoImportJobService
