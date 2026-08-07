"""手动字幕导入作业 service。

承接字幕目录导入的异步触发、查询与失败文件的删除/重命名/重导。通用作业生命周期
下沉到 ``BaseImportJobService``，这里只保留字幕导入专属的无媒体库差异与执行入口。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from src.common.media_import_status import (
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
)
from src.model import SubtitleImportJob
from src.schema.catalog.subtitle_imports import (
    SubtitleImportJobListItemResource,
    SubtitleImportJobResource,
    SubtitleImportTriggerResponse,
)
from src.service.catalog.subtitle_import_service import SubtitleImportService
from src.service.transfers.shared.base_import_job_service import BaseImportJobService


class SubtitleImportJobService(BaseImportJobService):
    JOB_MODEL = SubtitleImportJob
    TASK_KEY = "subtitle_directory_import"
    MUTEX_PREFIX = "subtitle_import"
    LIST_RESOURCE = SubtitleImportJobListItemResource
    DETAIL_RESOURCE = SubtitleImportJobResource
    TRIGGER_RESPONSE = SubtitleImportTriggerResponse
    JOB_ID_FIELD = "subtitle_import_job_id"
    JOB_NOT_FOUND_CODE = "subtitle_import_job_not_found"
    JOB_NOT_FOUND_MESSAGE = "字幕导入作业不存在"
    CONFLICT_CODE = "subtitle_import_conflict"
    LAUNCH_FAILED_CODE = "subtitle_import_failed"
    LAUNCH_FAILED_MESSAGE = "字幕导入任务入队失败"
    TRIGGER_TASK_NAME_PREFIX = "字幕导入"
    RETRY_TASK_NAME_PREFIX = "重导字幕 #"
    RERUN_TASK_NAME_PREFIX = "重跑字幕导入 #"
    # 字幕资产跟影片走、不归属媒体库，基类据此关闭 library 参与。
    LIBRARY_REQUIRED = False
    INTERRUPTED_FAILURE_DETAIL = "字幕导入进程已中断，作业未完成"
    INTERRUPTED_RECOVER_MESSAGE = "字幕导入进程已中断，作业已失败"
    LOG_LABEL = "Subtitle import"
    RECOVER_LOG_LABEL = "Recovered orphaned subtitle import"
    REQUIRED_LIBRARY_BACKEND = None

    @classmethod
    def trigger_directory_import(cls, source_path: str) -> SubtitleImportTriggerResponse:
        return cls._do_trigger(None, source_path, transfer_mode="auto")

    @classmethod
    def _create_job(
        cls,
        *,
        library,
        resolved_source: Path,
        transfer_mode: str,
        **launch_kwargs,
    ):
        return SubtitleImportJob.create(
            source_path=str(resolved_source),
            state=IMPORT_JOB_STATE_PENDING,
            transfer_mode=transfer_mode,
        )

    @classmethod
    def execute_from_queue(cls, reporter, params: dict) -> dict:
        job = cls._require_job(int(params["subtitle_import_job_id"]))
        try:
            result_job = SubtitleImportService().import_subtitles_from_source(
                job.source_path,
                job.id,
                progress_callback=reporter.progress_callback,
                only_files=params.get("only_files"),
            )
        except Exception as exc:
            cls._mark_import_failed(job.id, str(exc))
            logger.exception(
                "Subtitle directory import failed subtitle_import_job_id={} source_path={}",
                job.id,
                job.source_path,
            )
            raise
        return {
            "subtitle_import_job_id": result_job.id,
            "imported_count": result_job.imported_count,
            "skipped_count": result_job.skipped_count,
            "failed_count": result_job.failed_count,
            "job_state": result_job.state,
        }

    @classmethod
    def _orphan_jobs_query(cls):
        return (
            SubtitleImportJob.select()
            .where(
                SubtitleImportJob.state.in_(
                    (IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)
                )
            )
            .order_by(SubtitleImportJob.id.asc())
        )
