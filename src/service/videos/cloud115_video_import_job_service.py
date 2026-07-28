"""cloud115 普通视频导入作业编排与远程失败文件操作。"""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import List

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import SUPPORTED_VIDEO_EXTENSIONS
from src.common.media_import_status import (
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
)
from src.lib.cloud115 import Cloud115Error, Cloud115NotFoundError
from src.model import MediaLibrary, VideoImportJob
from src.model.enums import MediaLibraryBackend
from src.schema.videos.imports import (
    VideoImportJobListItemResource,
    VideoImportJobResource,
    VideoImportTriggerResponse,
)
from src.service.cloud115 import (
    assert_cid_outside_library_root,
    cloud115_client_for,
    map_cloud115_error,
    require_cloud115_library,
)
from src.service.system import ActivityService
from src.service.transfers.base_import_job_service import BaseImportJobService
from src.service.transfers.cloud115_import_common import (
    collect_cloud115_source_files,
    normalize_cloud115_transfer_mode,
    verify_cloud115_renamed_file,
)
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.service.videos.cloud115_video_import_service import Cloud115VideoImportService
from src.service.videos.video_import_job_service import VideoImportJobService


class Cloud115VideoImportJobService(BaseImportJobService):
    REQUIRED_LIBRARY_BACKEND = MediaLibraryBackend.CLOUD115
    JOB_MODEL = VideoImportJob
    TASK_KEY = "video_directory_import"
    LIST_RESOURCE = VideoImportJobListItemResource
    DETAIL_RESOURCE = VideoImportJobResource
    TRIGGER_RESPONSE = VideoImportTriggerResponse
    JOB_ID_FIELD = "video_import_job_id"
    JOB_NOT_FOUND_CODE = "video_import_job_not_found"
    JOB_NOT_FOUND_MESSAGE = "视频导入作业不存在"
    CONFLICT_CODE = "video_import_conflict"
    LAUNCH_FAILED_CODE = "video_import_failed"
    LAUNCH_FAILED_MESSAGE = "115 视频导入任务入队失败"
    TRIGGER_TASK_NAME_PREFIX = "115 视频导入"
    RETRY_TASK_NAME_PREFIX = "重导 115 失败视频 #"
    INTERRUPTED_FAILURE_DETAIL = "115 视频导入进程已中断，作业未完成"
    INTERRUPTED_RECOVER_MESSAGE = "115 视频导入进程已中断，作业已失败"
    LOG_LABEL = "Cloud115 video import"
    RECOVER_LOG_LABEL = "Recovered orphaned cloud115 video import"

    @classmethod
    def trigger_cloud115_import(
        cls,
        library_id: int,
        *,
        source_cid: str | None = None,
        source_fid: str | None = None,
        transfer_mode: str = "copy",
        collection_id: int | None = None,
    ) -> VideoImportTriggerResponse:
        if (source_cid is None) == (source_fid is None):
            raise ApiError(
                422,
                "invalid_cloud115_video_source",
                "source_cid 与 source_fid 必须恰好提供一个",
            )
        try:
            transfer_mode = normalize_cloud115_transfer_mode(
                transfer_mode, allow_legacy_move=False
            )
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_transfer_mode",
                "cloud115 视频导入模式必须是 copy 或 cleanup-source",
                {"transfer_mode": transfer_mode},
            ) from exc
        library = cls._require_library(library_id)
        VideoImportJobService._validate_collection(collection_id)
        source_label = cls._validate_source(
            library, source_cid=source_cid, source_fid=source_fid
        )
        return cls._launch_import(
            library=library,
            resolved_source=source_cid or source_fid,
            transfer_mode=transfer_mode,
            mutex_key=None,
            only_files=None,
            task_name=f"{cls.TRIGGER_TASK_NAME_PREFIX} {source_label}",
            source_cid=source_cid,
            source_fid=source_fid,
            collection_id=collection_id,
        )

    @staticmethod
    def _validate_source(
        library: MediaLibrary,
        *,
        source_cid: str | None,
        source_fid: str | None,
        require_video: bool = True,
    ) -> str:
        config = require_cloud115_library(library)

        async def _check() -> str:
            async with cloud115_client_for(library) as client:
                if source_cid is not None:
                    await assert_cid_outside_library_root(
                        client, source_cid=source_cid, root_cid=config["root_cid"]
                    )
                    return (await client.dir_info(source_cid)).name
                meta = await client.file_info(source_fid or "")
                try:
                    await Cloud115VideoImportService._assert_file_outside_library_root(
                        client,
                        parent_cid=meta.parent_id,
                        root_cid=config["root_cid"],
                    )
                except ValueError as exc:
                    raise ApiError(
                        422,
                        "cloud115_source_inside_library",
                        "导入文件不能位于媒体库管理目录内部",
                        {"source_fid": source_fid, "root_cid": config["root_cid"]},
                    ) from exc
                if (
                    require_video
                    and Cloud115VideoImportService._suffix(meta.name)
                    not in SUPPORTED_VIDEO_EXTENSIONS
                ):
                    raise ApiError(
                        422,
                        "import_source_unsupported",
                        "Source file is not a supported video",
                        {"source_fid": source_fid},
                    )
                return meta.name

        try:
            return asyncio.run(_check())
        except ApiError:
            raise
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    @classmethod
    def _create_job(
        cls,
        *,
        library,
        resolved_source,
        transfer_mode,
        source_cid=None,
        source_fid=None,
        collection_id=None,
        **launch_kwargs,
    ):
        return VideoImportJob.create(
            source_path=f"cloud115:{resolved_source}",
            source_cid=source_cid,
            source_fid=source_fid,
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
        source_cid=None,
        source_fid=None,
        collection_id=None,
        **launch_kwargs,
    ) -> None:
        DownloadImportRunner.submit(
            import_job.id,
            cls._run_import_job,
            library.id,
            source_cid,
            source_fid,
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
        source_cid: str | None,
        source_fid: str | None,
        video_import_job_id: int,
        task_run_id: int,
        transfer_mode: str,
        collection_id: int | None,
        only_files: List[str] | None,
    ) -> dict:
        ensure_database_ready()
        try:
            def _run_task(reporter):
                job = Cloud115VideoImportService().import_from_cloud115(
                    library_id,
                    source_cid=source_cid,
                    source_fid=source_fid,
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
                "Cloud115 video import failed video_import_job_id={}",
                video_import_job_id,
            )
            return {
                "video_import_job_id": video_import_job_id,
                "job_state": IMPORT_JOB_STATE_FAILED,
            }

    @classmethod
    def retry_failed_files(cls, job_id: int, files: List[str] | None = None):
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        if not job.source_cid and not job.source_fid:
            raise ApiError(422, "not_cloud115_video_import_job", "该作业不是 115 视频导入作业")
        actionable = cls._actionable_failed_paths(job)
        resolved_files = sorted(actionable) if files is None else list(files)
        for candidate in resolved_files:
            if candidate not in actionable:
                raise ApiError(403, "file_not_in_failed_list", "只能重导失败列表中的文件", {"path": candidate})
        if not resolved_files:
            raise ApiError(422, "no_retry_files", "没有可重导的失败文件")
        library = cls._require_library(job.library_id)
        cls._validate_source(
            library, source_cid=job.source_cid, source_fid=job.source_fid
        )
        return cls._launch_import(
            library=library,
            resolved_source=job.source_cid or job.source_fid,
            transfer_mode=normalize_cloud115_transfer_mode(
                job.transfer_mode or "copy", allow_legacy_move=False
            ),
            mutex_key=None,
            only_files=resolved_files,
            task_name=f"{cls.RETRY_TASK_NAME_PREFIX}{job_id}",
            source_cid=job.source_cid,
            source_fid=job.source_fid,
            collection_id=job.collection_id,
        )

    @classmethod
    async def _find_source_entry(cls, client, job, path: str):
        if job.source_fid:
            try:
                meta = await client.file_info(job.source_fid)
            except Cloud115NotFoundError:
                return None
            return meta if meta.name == path else None
        if not job.source_cid:
            return None
        # 只解析同名候选文件的父目录：按路径定位单个文件不需要还原整棵树的目录名。
        target_name = path.rsplit("/", 1)[-1]
        entries, rel_dirs = await collect_cloud115_source_files(
            client,
            job.source_cid,
            needs_rel_path=lambda entry: entry.name == target_name,
        )
        for entry in entries:
            if entry.name != target_name:
                continue
            rel_parts = rel_dirs[entry.parent_id]
            if "/".join([*rel_parts, entry.name]) == path:
                return entry
        return None

    @classmethod
    def delete_failed_file(cls, job_id: int, path: str):
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        cls._require_actionable_failed_entry(job, path)
        library = cls._require_library(job.library_id)
        # 单文件已经被用户从 115 删除时，删除操作本身仍应幂等成功；目录来源则仍需
        # 先确认整个来源目录位于媒体库管理目录外。
        if job.source_cid:
            cls._validate_source(library, source_cid=job.source_cid, source_fid=None)

        async def _delete() -> None:
            async with cloud115_client_for(library) as client:
                entry = await cls._find_source_entry(client, job, path)
                if entry is not None:
                    if job.source_fid:
                        config = require_cloud115_library(library)
                        try:
                            await Cloud115VideoImportService._assert_file_outside_library_root(
                                client,
                                parent_cid=entry.parent_id,
                                root_cid=config["root_cid"],
                            )
                        except ValueError as exc:
                            raise ApiError(
                                422,
                                "cloud115_source_inside_library",
                                "导入文件不能位于媒体库管理目录内部",
                                {
                                    "source_fid": job.source_fid,
                                    "root_cid": config["root_cid"],
                                },
                            ) from exc
                    fid = getattr(entry, "entry_id", None) or getattr(entry, "file_id")
                    await client.delete_files([fid])

        try:
            asyncio.run(_delete())
        except ApiError:
            raise
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc
        items = [item for item in cls._parse_failed_files(job) if item.get("path") != path]
        cls._save_failed_files(job, items)
        return cls.DETAIL_RESOURCE.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def rename_failed_file(cls, job_id: int, path: str, new_name: str):
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        cls._require_actionable_failed_entry(job, path)
        normalized = cls._validate_new_name(new_name)
        library = cls._require_library(job.library_id)
        cls._validate_source(
            library,
            source_cid=job.source_cid,
            source_fid=job.source_fid,
            require_video=False,
        )

        async def _rename() -> None:
            async with cloud115_client_for(library) as client:
                entry = await cls._find_source_entry(client, job, path)
                if entry is None:
                    raise ApiError(
                        422,
                        "cannot_rename_non_file",
                        "失败源文件已不存在",
                        {"path": path},
                    )
                fid = getattr(entry, "entry_id", None) or getattr(entry, "file_id")
                await client.rename_file(fid, normalized)
                await verify_cloud115_renamed_file(client, fid, normalized)

        try:
            asyncio.run(_rename())
        except ApiError:
            raise
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc
        new_path = str(PurePosixPath(path).with_name(normalized))
        items = cls._parse_failed_files(job)
        for item in items:
            if item.get("path") == path:
                item["path"] = new_path
        if job.source_fid:
            job.source_path = str(PurePosixPath(job.source_path).with_name(normalized))
            job.save()
        cls._save_failed_files(job, items)
        return cls.DETAIL_RESOURCE.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def _orphan_jobs_query(cls):
        # VideoImportJobService 的查询覆盖全部 videos 作业，本类不重复回收。
        return VideoImportJob.select().where(False)
