"""可视化媒体导入作业 service（JAV 目录导入）。

负责本地目录导入的触发、查询，以及失败文件的删除/重命名/重导。通用作业生命周期下沉到
``BaseImportJobService``，这里只保留 JAV 专属的归属模型、错误码文案、后台执行入口与孤儿筛选。

安全约束：
- 浏览/导入/删除/重命名/重导的路径都必须落在 ``media_import.browse_roots`` 白名单内。
- 删除/重命名/重导只允许作用于该作业失败列表中 ``kind=file`` 的单文件失败项，且作业须处于终态。
"""


from loguru import logger

from src.common.media_import_status import (
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
)
from src.model import ImportJob
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.media_import import (
    ImportJobListItemResource,
    ImportJobResource,
    ImportJobTriggerResponse,
)
from src.service.transfers.shared.base_import_job_service import BaseImportJobService
from src.service.transfers.imports.import_service import MediaImportService


class MediaImportJobService(BaseImportJobService):
    REQUIRED_LIBRARY_BACKEND = MediaLibraryBackend.LOCAL
    JOB_MODEL = ImportJob
    TASK_KEY = "media_directory_import"
    MUTEX_PREFIX = "media_import"
    LIST_RESOURCE = ImportJobListItemResource
    DETAIL_RESOURCE = ImportJobResource
    TRIGGER_RESPONSE = ImportJobTriggerResponse
    JOB_ID_FIELD = "import_job_id"
    JOB_NOT_FOUND_CODE = "import_job_not_found"
    JOB_NOT_FOUND_MESSAGE = "导入作业不存在"
    CONFLICT_CODE = "media_import_conflict"
    LAUNCH_FAILED_CODE = "media_import_failed"
    LAUNCH_FAILED_MESSAGE = "媒体导入任务入队失败"
    TRIGGER_TASK_NAME_PREFIX = "目录导入"
    RETRY_TASK_NAME_PREFIX = "重导失败文件 #"
    INTERRUPTED_FAILURE_DETAIL = "导入进程已中断，作业未完成"
    INTERRUPTED_RECOVER_MESSAGE = "媒体导入进程已中断，作业已失败"
    LOG_LABEL = "Media import"
    RECOVER_LOG_LABEL = "Recovered orphaned media import"

    @classmethod
    def trigger_directory_import(
        cls,
        library_id: int,
        source_path: str,
        transfer_mode: str = "auto",
    ) -> ImportJobTriggerResponse:
        return cls._do_trigger(library_id, source_path, transfer_mode=transfer_mode)

    @classmethod
    def _create_job(cls, *, library, resolved_source, transfer_mode, download_task_id=None, **launch_kwargs):
        return ImportJob.create(
            source_path=str(resolved_source),
            library=library,
            state=IMPORT_JOB_STATE_PENDING,
            transfer_mode=transfer_mode,
            download_task=download_task_id,
        )

    @classmethod
    def _launch_kwargs_from_retry(cls, job) -> dict:
        # 重导/重跑必须继承 download_task 关联，否则订阅页「取最新导入作业」会断链。
        return {"download_task_id": job.download_task_id}

    @classmethod
    def rerun_job(cls, job_id: int) -> ImportJobTriggerResponse:
        """整作业重跑：不论上次成败按原源整目录重扫——"completed 零产出"的恢复通路。"""
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        library = cls._require_library(job.library_id)
        resolved_source = cls._resolve_source_path(job.source_path)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            transfer_mode=job.transfer_mode or "auto",
            mutex_key=f"{cls.MUTEX_PREFIX}:rerun:{library.id}:{job_id}",
            only_files=None,
            task_name=f"重跑导入 #{job_id}",
            **cls._launch_kwargs_from_retry(job),
        )

    @classmethod
    def execute_from_queue(cls, reporter, params: dict) -> dict:
        job = cls._require_job(int(params["import_job_id"]))
        try:
            service = MediaImportService()
            result_job = service.import_from_source(
                job.source_path,
                job.library_id,
                import_job_id=job.id,
                progress_callback=reporter.progress_callback,
                transfer_mode=job.transfer_mode or "auto",
                only_files=params.get("only_files"),
            )
        except Exception as exc:
            cls._mark_import_failed(job.id, str(exc))
            logger.exception(
                "Media directory import failed import_job_id={} source_path={}",
                job.id,
                job.source_path,
            )
            raise
        return {
            "import_job_id": result_job.id,
            "imported_count": result_job.imported_count,
            "skipped_count": result_job.skipped_count,
            "failed_count": result_job.failed_count,
            "job_state": result_job.state,
            "new_playable_movies": reporter.summary.get("new_playable_movies", []),
        }

    @classmethod
    def _orphan_jobs_query(cls):
        # 只回收无关联下载任务的手动导入；下载导入由 DownloadSyncService 单独回收，避免重复处理。
        # cloud115 导入作业（source_cid 非空）同样是 download_task 为空的 ImportJob，一并覆盖。
        return (
            ImportJob.select()
            .where(
                ImportJob.download_task.is_null(True),
                ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)),
            )
            .order_by(ImportJob.id.asc())
        )


def import_job_service_for(job_id: int):
    """按作业类型分派 service：cloud115 作业（source_cid 非空）走 Cloud115ImportJobService。

    作业不存在时返回本地侧 service，由其统一抛 404，保持错误形状一致。
    """
    from src.service.transfers.cloud115.importer.job_service import (
        Cloud115ImportJobService,
    )

    job = ImportJob.get_or_none(ImportJob.id == job_id)
    if job is not None and job.source_cid:
        return Cloud115ImportJobService
    return MediaImportJobService
