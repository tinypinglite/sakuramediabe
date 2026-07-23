"""cloud115 导入作业 service（JAV，115 源目录 → cloud115 媒体库）。

与本地 ``MediaImportJobService`` 共用 ImportJob 模型、task_key、资源 schema 与孤儿回收
（cloud115 作业同样是 download_task 为空的 ImportJob，被本地侧的回收查询天然覆盖），
所以列表/详情/进度流接口零改动。差异点：

- 触发入参是 source_cid（115 目录）而非本地路径；transfer_mode 是 copy/cleanup-source。
- 触发前打 115 API 做防御校验（源目录与库管理目录互不包含）——前端已禁选，服务端兜底。
- mutex 按 **库** 锁而非（库 + 源）：115 的写操作（copy/rename/delete）是账号级串行约束，
  同一 cloud115 库同时只允许一个导入任务。
- 失败文件操作：仅支持重导（按源内相对路径子集重新枚举）；删除/重命名源文件是对用户
  115 目录的写操作，首版不提供（返回 422）。
"""

from __future__ import annotations

import asyncio
from typing import List

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
)
from src.model import ImportJob, MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.media_import import (
    ImportJobListItemResource,
    ImportJobResource,
    ImportJobTriggerResponse,
)
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    map_cloud115_error,
    require_cloud115_library,
)
from src.lib.cloud115 import Cloud115Error
from src.service.system import ActivityService
from src.service.transfers.base_import_job_service import BaseImportJobService
from src.service.transfers.cloud115_import_service import (
    CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    Cloud115ImportService,
    normalize_cloud115_transfer_mode,
)
from src.service.transfers.cloud115_import_common import cloud115_import_mutex_key
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready


class Cloud115ImportJobService(BaseImportJobService):
    REQUIRED_LIBRARY_BACKEND = MediaLibraryBackend.CLOUD115
    JOB_MODEL = ImportJob
    # 与本地目录导入共用 task_key：活动流/恢复链路把两者视作同一类任务。
    TASK_KEY = "media_directory_import"
    MUTEX_PREFIX = "media_import:cloud115"
    LIST_RESOURCE = ImportJobListItemResource
    DETAIL_RESOURCE = ImportJobResource
    TRIGGER_RESPONSE = ImportJobTriggerResponse
    JOB_ID_FIELD = "import_job_id"
    JOB_NOT_FOUND_CODE = "import_job_not_found"
    JOB_NOT_FOUND_MESSAGE = "导入作业不存在"
    CONFLICT_CODE = "media_import_conflict"
    LAUNCH_FAILED_CODE = "media_import_failed"
    LAUNCH_FAILED_MESSAGE = "媒体导入任务入队失败"
    TRIGGER_TASK_NAME_PREFIX = "115 网盘导入"
    RETRY_TASK_NAME_PREFIX = "重导 115 失败文件 #"
    INTERRUPTED_FAILURE_DETAIL = "导入进程已中断，作业未完成"
    INTERRUPTED_RECOVER_MESSAGE = "媒体导入进程已中断，作业已失败"
    LOG_LABEL = "Cloud115 import"
    RECOVER_LOG_LABEL = "Recovered orphaned cloud115 import"

    # ---- 触发 ----

    @classmethod
    def trigger_cloud115_import(
        cls,
        library_id: int,
        source_cid: str,
        transfer_mode: str = CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE,
    ) -> ImportJobTriggerResponse:
        try:
            transfer_mode = normalize_cloud115_transfer_mode(transfer_mode)
        except ValueError:
            raise ApiError(
                422, "invalid_transfer_mode",
                "cloud115 导入模式必须是 copy 或 cleanup-source",
                {"transfer_mode": transfer_mode},
            )
        if not source_cid or not source_cid.strip():
            raise ApiError(422, "invalid_source_cid", "source_cid 不能为空")
        source_cid = source_cid.strip()

        library = cls._require_library(library_id)
        try:
            require_cloud115_library(library)
        except ValueError as exc:
            raise ApiError(
                422, "media_library_backend_mismatch",
                "该媒体库不是 cloud115 库，不能按 source_cid 导入",
                {"library_id": library_id, "detail": str(exc)},
            ) from exc

        # 服务端防御：源目录与管理目录互不包含（前端禁选只是第一道）。
        source_name = cls._validate_source_and_fetch_name(library, source_cid)

        mutex_key = cloud115_import_mutex_key(library_id)
        return cls._launch_import(
            library=library,
            resolved_source=source_cid,   # cloud115 语境下 source 是 cid 字符串
            transfer_mode=transfer_mode,
            mutex_key=mutex_key,
            only_files=None,
            task_name=f"{cls.TRIGGER_TASK_NAME_PREFIX} {source_name or source_cid}",
        )

    @staticmethod
    def _validate_source_and_fetch_name(library: MediaLibrary, source_cid: str) -> str:
        """触发前的 115 侧校验：防御校验 + 拿源目录名（任务名展示用）。"""
        config = require_cloud115_library(library)

        async def _check() -> str:
            async with cloud115_client_for(library) as client:
                await assert_cid_outside_library_root(
                    client, source_cid=source_cid, root_cid=config["root_cid"]
                )
                meta = await client.dir_info(source_cid)
                return meta.name

        try:
            return asyncio.run(_check())
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    # ---- 重导（唯一支持的失败文件操作） ----

    @classmethod
    def retry_failed_files(cls, job_id: int, files: List[str] | None = None):
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        if not job.source_cid:
            raise ApiError(
                422, "not_cloud115_import_job",
                "该作业不是 cloud115 导入作业",
                {cls.JOB_ID_FIELD: job_id},
            )
        library = cls._require_library(job.library_id)
        actionable_paths = cls._actionable_failed_paths(job)

        if files is None:
            resolved_files = sorted(actionable_paths)
        else:
            for candidate in files:
                if candidate not in actionable_paths:
                    raise ApiError(
                        403, "file_not_in_failed_list",
                        "只能重导该导入作业失败列表中的可重导文件",
                        {"path": candidate},
                    )
            resolved_files = list(files)
        if not resolved_files:
            raise ApiError(422, "no_retry_files", "没有可重导的失败文件", {cls.JOB_ID_FIELD: job_id})

        # 首次导入和所有失败重导共享同一个库级写锁；115 copy/rename/delete 不允许并发。
        mutex_key = cloud115_import_mutex_key(library.id)
        return cls._launch_import(
            library=library,
            resolved_source=job.source_cid,
            transfer_mode=normalize_cloud115_transfer_mode(
                job.transfer_mode or CLOUD115_TRANSFER_MODE_CLEANUP_SOURCE
            ),
            mutex_key=mutex_key,
            only_files=resolved_files,
            task_name=f"{cls.RETRY_TASK_NAME_PREFIX}{job_id}",
        )

    # ---- 首版不支持的失败文件操作 ----

    @classmethod
    def delete_failed_file(cls, job_id: int, path: str):
        raise ApiError(
            422, "cloud115_failed_file_not_actionable",
            "cloud115 导入的失败文件不支持删除源文件，请在 115 App 内处理后重导",
            {cls.JOB_ID_FIELD: job_id, "path": path},
        )

    @classmethod
    def rename_failed_file(cls, job_id: int, path: str, new_name: str):
        raise ApiError(
            422, "cloud115_failed_file_not_actionable",
            "cloud115 导入的失败文件不支持重命名源文件，请在 115 App 内改名后重导",
            {cls.JOB_ID_FIELD: job_id, "path": path},
        )

    # ---- 基类钩子 ----

    @classmethod
    def _create_job(cls, *, library, resolved_source, transfer_mode, **launch_kwargs):
        transfer_mode = normalize_cloud115_transfer_mode(transfer_mode)
        return ImportJob.create(
            source_path=f"cloud115:{resolved_source}",
            source_cid=str(resolved_source),
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
        source_cid: str,
        import_job_id: int,
        task_run_id: int,
        transfer_mode: str,
        only_files: List[str] | None,
    ) -> dict:
        ensure_database_ready()
        try:
            def _run_task(reporter):
                service = Cloud115ImportService()
                job = service.import_from_cloud115(
                    library_id,
                    source_cid,
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
                "Cloud115 import failed import_job_id={} source_cid={}",
                import_job_id, source_cid,
            )
            return {
                "import_job_id": import_job_id,
                "job_state": IMPORT_JOB_STATE_FAILED,
            }

    @classmethod
    def _orphan_jobs_query(cls):
        # 孤儿回收由 MediaImportJobService 统一处理（cloud115 作业同为 download_task 为空的
        # ImportJob，被其查询覆盖），本类不单独注册回收入口。
        return ImportJob.select().where(False)
