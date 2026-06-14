"""可视化媒体导入作业 service（JAV 目录导入）。

负责本地目录导入的触发、查询，以及失败文件的删除/重命名/重导。通用作业生命周期下沉到
``BaseImportJobService``，这里只保留 JAV 专属的归属模型、错误码文案、后台执行入口与孤儿筛选。

安全约束：
- 浏览/导入/删除/重命名/重导的路径都必须落在 ``media_import.browse_roots`` 白名单内。
- 删除/重命名/重导只允许作用于该作业失败列表中 ``kind=file`` 的单文件失败项，且作业须处于终态。
"""

from typing import List

from loguru import logger

from src.common.media_import_status import (
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
)
from src.model import ImportJob
from src.schema.transfers.media_import import (
    ImportJobListItemResource,
    ImportJobResource,
    ImportJobTriggerResponse,
)
from src.service.system import ActivityService
from src.service.transfers.base_import_job_service import BaseImportJobService
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.service.transfers.media_import_service import MediaImportService


class MediaImportJobService(BaseImportJobService):
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
    def _create_job(cls, *, library, resolved_source, transfer_mode, **launch_kwargs):
        return ImportJob.create(
            source_path=str(resolved_source),
            library=library,
            state=IMPORT_JOB_STATE_PENDING,
            transfer_mode=transfer_mode,
        )

    @classmethod
    def _submit_runner(
        cls, *, import_job, task_run, library, resolved_source, transfer_mode, only_files, **launch_kwargs
    ) -> None:
        DownloadImportRunner.submit(
            import_job.id,
            cls._run_import_job,
            library.id,
            str(resolved_source),
            import_job.id,
            task_run.id,
            transfer_mode,
            only_files,
        )

    @classmethod
    def _run_import_job(
        cls,
        library_id: int,
        source_path: str,
        import_job_id: int,
        task_run_id: int,
        transfer_mode: str,
        only_files: List[str] | None,
    ) -> dict:
        ensure_database_ready()
        try:
            def _run_task(reporter):
                service = MediaImportService()
                job = service.import_from_source(
                    source_path,
                    library_id,
                    import_job_id=import_job_id,
                    progress_callback=reporter.progress_callback,
                    transfer_mode=transfer_mode,
                    only_files=only_files,
                )
                return {
                    "import_job_id": job.id,
                    "imported_count": job.imported_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "job_state": job.state,
                    "new_playable_movies": reporter.summary.get("new_playable_movies", []),
                }

            return ActivityService.run_task(
                task_key=cls.TASK_KEY,
                task_name=None,
                trigger_type="internal",
                task_run_id=task_run_id,
                func=_run_task,
            )
        except Exception as exc:
            cls._mark_import_failed(import_job_id, str(exc))
            logger.exception(
                "Media directory import failed import_job_id={} source_path={}",
                import_job_id,
                source_path,
            )
            return {
                "import_job_id": import_job_id,
                "job_state": IMPORT_JOB_STATE_FAILED,
            }

    @classmethod
    def _orphan_jobs_query(cls):
        # 只回收无关联下载任务的手动导入；下载导入由 DownloadSyncService 单独回收，避免重复处理。
        return (
            ImportJob.select()
            .where(
                ImportJob.download_task.is_null(True),
                ImportJob.state.in_((IMPORT_JOB_STATE_PENDING, IMPORT_JOB_STATE_RUNNING)),
            )
            .order_by(ImportJob.id.asc())
        )
