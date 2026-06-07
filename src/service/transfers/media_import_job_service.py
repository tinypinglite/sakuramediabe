"""可视化媒体导入作业 service。

负责本地目录导入的触发、查询，以及失败文件的删除/重命名/重导。
后台执行复用 ``DownloadImportRunner`` 线程池与 ``ActivityService`` 任务运行链路，
触发防重依赖 ``BackgroundTaskRun.mutex_key`` 唯一约束。

安全约束：
- 浏览/导入/删除/重命名/重导的路径都必须落在 ``media_import.browse_roots`` 白名单内。
- 删除/重命名/重导只允许作用于该作业失败列表中 ``kind=file`` 的单文件失败项，且作业须处于终态。
"""

import hashlib
import json
from pathlib import Path
from typing import Any, List

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.fs_browse import assert_within_allowed_roots, normalize_abs_path
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import ImportJob, MediaLibrary
from src.schema.common.pagination import PageResponse
from src.schema.transfers.media_import import (
    FailedFileResource,
    ImportJobListItemResource,
    ImportJobResource,
    ImportJobTriggerResponse,
)
from src.service.system import ActivityService
from src.service.transfers.import_runner import DownloadImportRunner, ensure_database_ready
from src.service.transfers.media_import_service import (
    FAILED_FILE_KIND_FILE,
    MediaImportService,
    classify_failed_file_kind,
    make_failure_item,
)

TASK_KEY = "media_directory_import"
# 仅终态作业才允许执行失败文件的删除/重命名/重导，避免与运行中的导入线程竞争。
TERMINAL_JOB_STATES = ("completed", "failed")


class MediaImportJobService:
    @classmethod
    def trigger_directory_import(
        cls,
        library_id: int,
        source_path: str,
        transfer_mode: str = "auto",
    ) -> ImportJobTriggerResponse:
        if transfer_mode not in ("auto", "cleanup-source"):
            raise ApiError(
                422,
                "invalid_transfer_mode",
                "无效的导入模式",
                {"transfer_mode": transfer_mode},
            )

        library = cls._require_library(library_id)
        resolved_source = cls._resolve_source_path(source_path)
        mutex_key = cls._build_mutex_key(library_id, resolved_source)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            transfer_mode=transfer_mode,
            mutex_key=mutex_key,
            only_files=None,
            task_name=f"目录导入 {resolved_source.name or resolved_source}",
        )

    @classmethod
    def retry_failed_files(
        cls,
        import_job_id: int,
        files: List[str] | None = None,
    ) -> ImportJobTriggerResponse:
        job = cls._require_job(import_job_id)
        cls._assert_job_terminal(job)
        library = cls._require_library(job.library_id)
        # 只允许重导失败列表中“可操作的单文件失败项”（kind=file）。
        actionable_paths = cls._actionable_failed_paths(job)

        if files is None:
            resolved_files = sorted(actionable_paths)
        else:
            # 安全约束：每个待重导路径都必须是该作业可重导的失败文件。
            for candidate in files:
                if candidate not in actionable_paths:
                    raise ApiError(
                        403,
                        "file_not_in_failed_list",
                        "只能重导该导入作业失败列表中的可重导文件",
                        {"path": candidate},
                    )
            resolved_files = list(files)

        if not resolved_files:
            raise ApiError(
                422,
                "no_retry_files",
                "没有可重导的失败文件",
                {"import_job_id": import_job_id},
            )

        # 纵深防御：逐个待重导文件也必须落在白名单根目录内。
        for candidate in resolved_files:
            assert_within_allowed_roots(Path(candidate), settings.media_import.browse_roots)

        resolved_source = cls._resolve_source_path(job.source_path)
        mutex_key = cls._build_retry_mutex_key(import_job_id, library.id, resolved_source)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            # 重导沿用原作业的导入模式，避免 cleanup-source 作业被静默退化为不删源。
            transfer_mode=job.transfer_mode or "auto",
            mutex_key=mutex_key,
            only_files=resolved_files,
            task_name=f"重导失败文件 #{import_job_id}",
        )

    @classmethod
    def list_jobs(cls, *, page: int = 1, page_size: int = 20) -> PageResponse[ImportJobListItemResource]:
        if page < 1 or page_size < 1:
            raise ApiError(422, "invalid_pagination", "分页参数非法")
        query = ImportJob.select().order_by(ImportJob.id.desc())
        total = query.count()
        start = (page - 1) * page_size
        items = [
            ImportJobListItemResource.from_model(job)
            for job in query.offset(start).limit(page_size)
        ]
        return PageResponse[ImportJobListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_job(cls, import_job_id: int) -> ImportJobResource:
        job = cls._require_job(import_job_id)
        return ImportJobResource.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def delete_failed_file(cls, import_job_id: int, path: str) -> ImportJobResource:
        job = cls._require_job(import_job_id)
        cls._assert_job_terminal(job)
        cls._require_actionable_failed_entry(job, path)
        cls._assert_within_allowed_roots(path)

        target = Path(path)
        # 目录类失败项（如任务级失败写入的源目录）禁止按文件删除，避免误删整目录。
        if target.is_dir():
            raise ApiError(422, "cannot_delete_directory", "该失败项是目录，不能按文件删除", {"path": path})

        try:
            target.unlink()
            logger.info("Media import failed file deleted import_job_id={} path={}", import_job_id, path)
        except FileNotFoundError:
            # 文件已不存在时视为删除成功，仍从失败列表移除该条记录。
            logger.info("Media import failed file already missing import_job_id={} path={}", import_job_id, path)
        except OSError as exc:
            raise ApiError(
                422,
                "delete_failed_file_failed",
                "删除失败文件出错",
                {"path": path, "detail": str(exc)},
            ) from exc

        failure_items = [item for item in cls._parse_failed_files(job) if item.get("path") != path]
        cls._save_failed_files(job, failure_items)
        return ImportJobResource.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def rename_failed_file(cls, import_job_id: int, path: str, new_name: str) -> ImportJobResource:
        job = cls._require_job(import_job_id)
        cls._assert_job_terminal(job)
        cls._require_actionable_failed_entry(job, path)
        cls._assert_within_allowed_roots(path)

        normalized_new_name = cls._validate_new_name(new_name)

        source = Path(path)
        # 仅允许重命名常规文件，杜绝把目录类失败项整目录改名。
        if not source.is_file():
            raise ApiError(422, "cannot_rename_non_file", "该失败项不是常规文件，不能重命名", {"path": path})

        target = source.parent / normalized_new_name
        if target.exists():
            raise ApiError(409, "rename_target_exists", "目标文件已存在", {"path": str(target)})

        try:
            source.rename(target)
        except OSError as exc:
            raise ApiError(
                422,
                "rename_failed_file_failed",
                "重命名失败文件出错",
                {"path": path, "detail": str(exc)},
            ) from exc
        logger.info(
            "Media import failed file renamed import_job_id={} from={} to={}",
            import_job_id,
            path,
            str(target),
        )

        # 把失败列表中该条记录的路径更新为新路径，保证后续仍能对新名重导且满足“仅限失败列表内”约束。
        failure_items = cls._parse_failed_files(job)
        for item in failure_items:
            if item.get("path") == path:
                item["path"] = str(target)
        cls._save_failed_files(job, failure_items)
        return ImportJobResource.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def recover_orphaned_jobs(cls) -> dict:
        """回收中断的手动目录导入作业。

        把没有存活 owner 进程、也没有活跃后台线程的 pending/running 手动导入作业复位为 failed，
        并补一条任务级失败记录；对应 task_run 一并回收。下载导入由 download 侧 handler 负责，这里只处理
        无关联下载任务的手动导入，避免与 ``DownloadSyncService._recover_orphaned_imports`` 重复处理。
        """
        recovered_count = 0
        orphaned_jobs = (
            ImportJob.select()
            .where(
                ImportJob.download_task.is_null(True),
                ImportJob.state.in_(("pending", "running")),
            )
            .order_by(ImportJob.id.asc())
        )
        for job in orphaned_jobs:
            # 仍有存活 owner 进程或活跃后台线程时跳过，避免误杀正在运行的导入。
            if cls._has_live_owner(job) or DownloadImportRunner.has_active_job(job.id):
                continue
            failure_items = cls._parse_failed_files(job)
            failure_items.append(
                make_failure_item(job.source_path, "import_job_interrupted", "导入进程已中断，作业未完成")
            )
            job.failed_count = max(job.failed_count, 1)
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.state = "failed"
            job.finished_at = utc_now_for_db()
            job.updated_at = utc_now_for_db()
            job.save()
            if job.task_run_id is not None:
                ActivityService.recover_task_run(
                    job.task_run_id,
                    error_message="媒体导入进程已中断，作业已失败",
                    result_summary={"import_job_id": job.id},
                    allow_null_owner=True,
                )
            recovered_count += 1
            logger.warning(
                "Recovered orphaned media import job_id={} source_path={}",
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
        mutex_key: str,
        only_files: List[str] | None,
        task_name: str,
    ) -> ImportJobTriggerResponse:
        try:
            task_run = ActivityService.create_task_run(
                task_key=TASK_KEY,
                task_name=task_name,
                trigger_type="manual",
                mutex_key=mutex_key,
            )
        except IntegrityError as exc:
            # mutex_key 唯一约束命中，说明同库同源（或同作业重导）已有进行中的任务。
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "media_import_conflict",
                "相同导入源已有进行中的任务",
                {
                    "mutex_key": mutex_key,
                    "blocking_task_run_id": blocking.id if blocking is not None else None,
                },
            ) from exc

        # task_run 建好后到入队成功之间任一步骤失败，都必须回收 task_run（释放 mutex_key），
        # 否则该 mutex_key 会长期占用导致同库同源永久 409。
        import_job: ImportJob | None = None
        try:
            import_job = ImportJob.create(
                source_path=str(resolved_source),
                library=library,
                state="pending",
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
                only_files,
            )
        except Exception as exc:
            import_job_id = import_job.id if import_job is not None else None
            if import_job is not None:
                # 与其它失败路径保持一致：终态作业补一条任务级失败明细，避免详情页 failed 却无失败原因。
                failure_items = cls._parse_failed_files(import_job)
                failure_items.append(
                    make_failure_item(resolved_source, "import_job_bootstrap_failed", str(exc))
                )
                import_job.failed_count = max(import_job.failed_count, 1)
                import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
                import_job.state = "failed"
                import_job.finished_at = utc_now_for_db()
                import_job.save()
            ActivityService.fail_task_run(
                task_run.id,
                error_message=str(exc),
                result_summary={"import_job_id": import_job_id},
            )
            raise ApiError(
                502,
                "media_import_failed",
                "媒体导入任务入队失败",
                {"detail": str(exc), "import_job_id": import_job_id},
            ) from exc

        return ImportJobTriggerResponse(
            import_job_id=import_job.id,
            task_run_id=task_run.id,
            status="accepted",
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
                task_key=TASK_KEY,
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
                "job_state": "failed",
            }

    @staticmethod
    def _mark_import_failed(import_job_id: int, detail: str) -> None:
        import_job = ImportJob.get_or_none(ImportJob.id == import_job_id)
        if import_job is None:
            return
        failure_items: list[dict[str, Any]] = []
        try:
            if import_job.failed_files:
                failure_items = json.loads(import_job.failed_files)
        except json.JSONDecodeError:
            failure_items = []
        failure_items.append(
            make_failure_item(import_job.source_path, "import_job_bootstrap_failed", detail)
        )
        import_job.failed_count = max(import_job.failed_count, 1)
        import_job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        import_job.state = "failed"
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
    def _require_job(import_job_id: int) -> ImportJob:
        job = ImportJob.get_or_none(ImportJob.id == import_job_id)
        if job is None:
            raise ApiError(404, "import_job_not_found", "导入作业不存在", {"import_job_id": import_job_id})
        return job

    @staticmethod
    def _assert_job_terminal(job: ImportJob) -> None:
        if job.state not in TERMINAL_JOB_STATES:
            raise ApiError(
                409,
                "job_in_progress",
                "导入作业进行中，暂不能操作失败文件",
                {"import_job_id": job.id, "state": job.state},
            )

    @staticmethod
    def _has_live_owner(job: ImportJob) -> bool:
        task_run = job.task_run
        if task_run is None or task_run.owner_pid is None:
            return False
        return ActivityService._is_process_alive(task_run.owner_pid)

    @staticmethod
    def _validate_new_name(new_name: str) -> str:
        normalized = (new_name or "").strip()
        if not normalized or normalized in (".", ".."):
            raise ApiError(422, "invalid_new_name", "新文件名非法", {"new_name": new_name})
        if "/" in normalized or "\\" in normalized:
            raise ApiError(422, "invalid_new_name", "新文件名不能包含路径分隔符", {"new_name": new_name})
        if normalized.startswith("."):
            raise ApiError(422, "invalid_new_name", "新文件名不能以点开头", {"new_name": new_name})
        if any(ord(ch) < 32 for ch in normalized):
            raise ApiError(422, "invalid_new_name", "新文件名包含非法控制字符", {"new_name": new_name})
        if len(normalized) > 255:
            raise ApiError(422, "invalid_new_name", "新文件名过长", {"new_name": new_name})
        return normalized

    @staticmethod
    def _resolve_source_path(source_path: str) -> Path:
        resolved = normalize_abs_path(source_path)
        assert_within_allowed_roots(resolved, settings.media_import.browse_roots)
        return resolved

    @staticmethod
    def _assert_within_allowed_roots(path: str) -> None:
        assert_within_allowed_roots(Path(path), settings.media_import.browse_roots)

    @staticmethod
    def _build_mutex_key(library_id: int, resolved_source: Path) -> str:
        digest = hashlib.sha1(str(resolved_source).encode("utf-8")).hexdigest()
        return f"media_import:{library_id}:{digest}"

    @staticmethod
    def _build_retry_mutex_key(import_job_id: int, library_id: int, resolved_source: Path) -> str:
        digest = hashlib.sha1(str(resolved_source).encode("utf-8")).hexdigest()
        return f"media_import:retry:{library_id}:{digest}:{import_job_id}"

    @staticmethod
    def _parse_failed_files(job: ImportJob) -> list[dict[str, Any]]:
        if not job.failed_files:
            return []
        try:
            items = json.loads(job.failed_files)
        except json.JSONDecodeError:
            return []
        return items if isinstance(items, list) else []

    @staticmethod
    def _entry_kind(item: dict[str, Any]) -> str:
        # 历史记录可能没有 kind 字段，按 reason 回推分类，保证旧数据兼容。
        kind = item.get("kind")
        if kind:
            return kind
        return classify_failed_file_kind(item.get("reason", ""))

    @classmethod
    def _failed_file_resources(cls, job: ImportJob) -> list[FailedFileResource]:
        return [
            FailedFileResource(
                path=item.get("path", ""),
                reason=item.get("reason", ""),
                detail=item.get("detail", ""),
                kind=cls._entry_kind(item),
            )
            for item in cls._parse_failed_files(job)
            if isinstance(item, dict)
        ]

    @classmethod
    def _actionable_failed_paths(cls, job: ImportJob) -> set[str]:
        return {
            item.get("path", "")
            for item in cls._parse_failed_files(job)
            if isinstance(item, dict) and item.get("path") and cls._entry_kind(item) == FAILED_FILE_KIND_FILE
        }

    @classmethod
    def _find_failed_entry(cls, job: ImportJob, path: str) -> dict[str, Any] | None:
        for item in cls._parse_failed_files(job):
            if isinstance(item, dict) and item.get("path") == path:
                return item
        return None

    @classmethod
    def _require_actionable_failed_entry(cls, job: ImportJob, path: str) -> dict[str, Any]:
        entry = cls._find_failed_entry(job, path)
        if entry is None:
            raise ApiError(
                403,
                "file_not_in_failed_list",
                "只能操作该导入作业失败列表中的文件",
                {"path": path},
            )
        kind = cls._entry_kind(entry)
        if kind != FAILED_FILE_KIND_FILE:
            raise ApiError(
                422,
                "failed_file_not_actionable",
                "该失败项不可执行删除/重命名/重导操作",
                {"path": path, "kind": kind},
            )
        return entry

    @staticmethod
    def _save_failed_files(job: ImportJob, failure_items: list[dict[str, Any]]) -> None:
        job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        job.updated_at = utc_now_for_db()
        job.save()
