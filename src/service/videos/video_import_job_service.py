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
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
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
    def _submit_runner(
        cls,
        *,
        import_job,
        task_run,
        library,
        resolved_source,
        transfer_mode,
        only_files,
        collection_id: int | None = None,
        **launch_kwargs,
    ) -> None:
        # collection_id 夹在 transfer_mode 与 only_files 之间，位置序列须与 _run_import_job 形参严格对齐。
        DownloadImportRunner.submit(
            import_job.id,
            cls._run_import_job,
            library.id,
            str(resolved_source),
            import_job.id,
            task_run.id,
            transfer_mode,
            collection_id,
            only_files,
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
        only_files: List[str] | None = None,
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
                    only_files=only_files,
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
                task_key=cls.TASK_KEY,
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

    @classmethod
    def _orphan_jobs_query(cls):
        return (
            VideoImportJob.select()
            .where(VideoImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)))
            .order_by(VideoImportJob.id.asc())
        )
