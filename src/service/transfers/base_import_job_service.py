"""目录导入作业 service 的公共基类（模板方法模式）。

JAV 目录导入（``MediaImportJobService``）与非 JAV 视频导入（``VideoImportJobService``）
共用同一套作业生命周期：异步触发（建作业 + BackgroundTaskRun，靠 mutex_key 唯一约束防重）、
分页查询、失败文件的删除/重命名/重导、以及进程中断后的孤儿作业回收。两者仅在
归属模型、错误码文案、是否带合集、是否需排除下载导入等少数维度上不同，这些差异通过
类属性与少量钩子方法下放给子类，骨架集中在此处维护，避免两份近乎逐行的拷贝各自演进失配。

子类必须设置的类属性见下方占位声明；必须实现的钩子：``_create_job``、``_submit_runner``、
``_run_import_job``、``_orphan_jobs_query``。可选 override 的钩子：``_pre_launch_validate``、
``_assert_transfer_mode_constraints``、``_launch_kwargs_from_trigger``、``_launch_kwargs_from_retry``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, List

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.fs_browse import assert_within_allowed_roots, normalize_abs_path
from src.common.media_import_status import (
    FAILED_FILE_KIND_FILE,
    FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED,
    FAILURE_REASON_IMPORT_JOB_INTERRUPTED,
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    TERMINAL_JOB_STATES,
    classify_failed_file_kind,
    make_failure_item,
)
from src.common.process import is_process_alive
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.schema.common.pagination import PageResponse
from src.schema.transfers.media_import import FailedFileResource
from src.service.system import ActivityService
from src.service.transfers.import_runner import DownloadImportRunner


class BaseImportJobService:
    # ---- 子类必须覆盖的类属性 ----
    JOB_MODEL: Any = None
    TASK_KEY: str = ""
    MUTEX_PREFIX: str = ""  # "media_import" / "video_import"
    LIST_RESOURCE: Any = None
    DETAIL_RESOURCE: Any = None
    TRIGGER_RESPONSE: Any = None
    # 触发响应/错误 details/result_summary 统一使用的作业 id 字段名。
    JOB_ID_FIELD: str = ""  # "import_job_id" / "video_import_job_id"
    # 错误码与中文文案。
    JOB_NOT_FOUND_CODE: str = ""
    JOB_NOT_FOUND_MESSAGE: str = ""
    CONFLICT_CODE: str = ""
    LAUNCH_FAILED_CODE: str = ""
    LAUNCH_FAILED_MESSAGE: str = ""
    # 任务名前缀。
    TRIGGER_TASK_NAME_PREFIX: str = ""  # "目录导入" / "视频导入"
    RETRY_TASK_NAME_PREFIX: str = ""  # "重导失败文件 #" / "重导失败视频 #"
    # 失败/恢复文案与日志标签。
    INTERRUPTED_FAILURE_DETAIL: str = ""
    INTERRUPTED_RECOVER_MESSAGE: str = ""
    LOG_LABEL: str = ""  # "Media import" / "Video import"
    RECOVER_LOG_LABEL: str = ""
    REQUIRED_LIBRARY_BACKEND: MediaLibraryBackend | None = None

    # ---- 公开查询 ----

    @classmethod
    def list_jobs(cls, *, page: int = 1, page_size: int = 20) -> PageResponse:
        if page < 1 or page_size < 1:
            raise ApiError(422, "invalid_pagination", "分页参数非法")
        query = cls.JOB_MODEL.select().order_by(cls.JOB_MODEL.id.desc())
        total = query.count()
        start = (page - 1) * page_size
        items = [
            cls.LIST_RESOURCE.from_model(job)
            for job in query.offset(start).limit(page_size)
        ]
        return PageResponse[cls.LIST_RESOURCE](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_job(cls, job_id: int):
        job = cls._require_job(job_id)
        return cls.DETAIL_RESOURCE.from_model(job, failed_files=cls._failed_file_resources(job))

    # ---- 失败文件删除 / 重命名 ----

    @classmethod
    def delete_failed_file(cls, job_id: int, path: str):
        job = cls._require_job(job_id)
        cls._assert_job_terminal(job)
        cls._require_actionable_failed_entry(job, path)
        cls._assert_within_allowed_roots(path)

        target = Path(path)
        # 目录类失败项（如任务级失败写入的源目录）禁止按文件删除，避免误删整目录。
        if target.is_dir():
            raise ApiError(422, "cannot_delete_directory", "该失败项是目录，不能按文件删除", {"path": path})

        try:
            target.unlink()
            logger.info("{} failed file deleted job_id={} path={}", cls.LOG_LABEL, job_id, path)
        except FileNotFoundError:
            # 文件已不存在时视为删除成功，仍从失败列表移除该条记录。
            logger.info("{} failed file already missing job_id={} path={}", cls.LOG_LABEL, job_id, path)
        except OSError as exc:
            raise ApiError(
                422,
                "delete_failed_file_failed",
                "删除失败文件出错",
                {"path": path, "detail": str(exc)},
            ) from exc

        failure_items = [item for item in cls._parse_failed_files(job) if item.get("path") != path]
        cls._save_failed_files(job, failure_items)
        return cls.DETAIL_RESOURCE.from_model(job, failed_files=cls._failed_file_resources(job))

    @classmethod
    def rename_failed_file(cls, job_id: int, path: str, new_name: str):
        job = cls._require_job(job_id)
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
        logger.info("{} failed file renamed job_id={} from={} to={}", cls.LOG_LABEL, job_id, path, str(target))

        # 把失败列表中该条记录的路径更新为新路径，保证后续仍能对新名重导且满足“仅限失败列表内”约束。
        failure_items = cls._parse_failed_files(job)
        for item in failure_items:
            if item.get("path") == path:
                item["path"] = str(target)
        cls._save_failed_files(job, failure_items)
        return cls.DETAIL_RESOURCE.from_model(job, failed_files=cls._failed_file_resources(job))

    # ---- 触发：目录导入 / 失败文件重导 ----

    @classmethod
    def _do_trigger(cls, library_id: int, source_path: str, *, transfer_mode: str, **trigger_kwargs):
        """目录导入触发骨架：子类公开方法转调本方法，差异通过钩子注入。"""
        if transfer_mode not in ("auto", "cleanup-source"):
            raise ApiError(422, "invalid_transfer_mode", "无效的导入模式", {"transfer_mode": transfer_mode})

        library = cls._require_library(library_id)
        cls._pre_launch_validate(**trigger_kwargs)
        resolved_source = cls._resolve_source_path(source_path)
        cls._assert_transfer_mode_constraints(resolved_source, transfer_mode, **trigger_kwargs)
        mutex_key = cls._build_mutex_key(library_id, resolved_source)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            transfer_mode=transfer_mode,
            mutex_key=mutex_key,
            only_files=None,
            task_name=f"{cls.TRIGGER_TASK_NAME_PREFIX} {resolved_source.name or resolved_source}",
            **cls._launch_kwargs_from_trigger(**trigger_kwargs),
        )

    @classmethod
    def retry_failed_files(cls, job_id: int, files: List[str] | None = None):
        job = cls._require_job(job_id)
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
            raise ApiError(422, "no_retry_files", "没有可重导的失败文件", {cls.JOB_ID_FIELD: job_id})

        # 纵深防御：逐个待重导文件也必须落在白名单根目录内。
        for candidate in resolved_files:
            assert_within_allowed_roots(Path(candidate), settings.media_import.browse_roots)

        resolved_source = cls._resolve_source_path(job.source_path)
        mutex_key = cls._build_retry_mutex_key(job_id, library.id, resolved_source)
        return cls._launch_import(
            library=library,
            resolved_source=resolved_source,
            # 重导沿用原作业的导入模式（与合集等子类专属归属），避免 cleanup-source 静默退化或丢失关联。
            transfer_mode=job.transfer_mode or "auto",
            mutex_key=mutex_key,
            only_files=resolved_files,
            task_name=f"{cls.RETRY_TASK_NAME_PREFIX}{job_id}",
            **cls._launch_kwargs_from_retry(job),
        )

    @classmethod
    def _launch_import(
        cls,
        *,
        library: MediaLibrary,
        resolved_source: Path,
        transfer_mode: str,
        mutex_key: str | None,
        only_files: List[str] | None,
        task_name: str,
        **launch_kwargs,
    ):
        try:
            task_run = ActivityService.create_task_run(
                task_key=cls.TASK_KEY,
                task_name=task_name,
                trigger_type="manual",
                mutex_key=mutex_key,
            )
        except IntegrityError as exc:
            if mutex_key is None:
                raise
            # mutex_key 唯一约束命中，说明同库同源（或同作业重导）已有进行中的任务。
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                cls.CONFLICT_CODE,
                "相同导入源已有进行中的任务",
                {
                    "mutex_key": mutex_key,
                    "blocking_task_run_id": blocking.id if blocking is not None else None,
                },
            ) from exc

        # task_run 建好后到入队成功之间任一步骤失败都必须回收 task_run；
        # 对带 mutex_key 的调用，这也会同步释放互斥键。
        import_job = None
        try:
            import_job = cls._create_job(
                library=library,
                resolved_source=resolved_source,
                transfer_mode=transfer_mode,
                **launch_kwargs,
            )
            import_job.task_run = task_run
            import_job.save()
            cls._submit_runner(
                import_job=import_job,
                task_run=task_run,
                library=library,
                resolved_source=resolved_source,
                transfer_mode=transfer_mode,
                only_files=only_files,
                **launch_kwargs,
            )
        except Exception as exc:
            import_job_id = import_job.id if import_job is not None else None
            if import_job is not None:
                # 与其它失败路径保持一致：终态作业补一条任务级失败明细，避免详情页 failed 却无失败原因。
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
                result_summary={cls.JOB_ID_FIELD: import_job_id},
            )
            raise ApiError(
                502,
                cls.LAUNCH_FAILED_CODE,
                cls.LAUNCH_FAILED_MESSAGE,
                {"detail": str(exc), cls.JOB_ID_FIELD: import_job_id},
            ) from exc

        return cls.TRIGGER_RESPONSE(
            **{cls.JOB_ID_FIELD: import_job.id},
            task_run_id=task_run.id,
            status="accepted",
        )

    @classmethod
    def _mark_import_failed(cls, job_id: int, detail: str) -> None:
        import_job = cls.JOB_MODEL.get_or_none(cls.JOB_MODEL.id == job_id)
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

    # ---- 孤儿作业回收 ----

    @classmethod
    def recover_orphaned_jobs(cls) -> dict:
        """回收中断的目录导入作业。

        把没有存活 owner 进程、也没有活跃后台线程的 pending/running 作业复位为 failed，
        并补一条任务级失败记录；对应 task_run 一并回收。具体筛选范围由 ``_orphan_jobs_query`` 决定。
        """
        recovered_count = 0
        for job in cls._orphan_jobs_query():
            # 仍有存活 owner 进程或活跃后台线程时跳过，避免误杀正在运行的导入。
            if cls._has_live_owner(job) or DownloadImportRunner.has_active_job(job.id):
                continue
            failure_items = cls._parse_failed_files(job)
            failure_items.append(
                make_failure_item(job.source_path, FAILURE_REASON_IMPORT_JOB_INTERRUPTED, cls.INTERRUPTED_FAILURE_DETAIL)
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
                    error_message=cls.INTERRUPTED_RECOVER_MESSAGE,
                    result_summary={cls.JOB_ID_FIELD: job.id},
                    allow_null_owner=True,
                )
            recovered_count += 1
            logger.warning("{} job_id={} source_path={}", cls.RECOVER_LOG_LABEL, job.id, job.source_path)
        return {"recovered_count": recovered_count}

    # ---- 子类钩子：差异点 ----

    @classmethod
    def _create_job(cls, *, library, resolved_source, transfer_mode, **launch_kwargs):
        raise NotImplementedError

    @classmethod
    def _submit_runner(cls, *, import_job, task_run, library, resolved_source, transfer_mode, only_files, **launch_kwargs) -> None:
        raise NotImplementedError

    @classmethod
    def _run_import_job(cls, *args, **kwargs) -> dict:
        raise NotImplementedError

    @classmethod
    def _orphan_jobs_query(cls):
        raise NotImplementedError

    @classmethod
    def _pre_launch_validate(cls, **trigger_kwargs) -> None:
        # 触发前的子类专属校验（如 videos 的合集存在性），基类默认无校验。
        return None

    @classmethod
    def _assert_transfer_mode_constraints(cls, resolved_source: Path, transfer_mode: str, **trigger_kwargs) -> None:
        # 导入模式相关的子类专属约束（如 videos 的 cleanup-source 不得指向媒体库内），基类默认无约束。
        return None

    @classmethod
    def _launch_kwargs_from_trigger(cls, **trigger_kwargs) -> dict:
        # 把触发参数中需透传给 _create_job/_submit_runner 的子类专属字段打包，基类默认无透传。
        return {}

    @classmethod
    def _launch_kwargs_from_retry(cls, job) -> dict:
        # 重导时从原作业继承的子类专属字段（如 videos 的合集归属），基类默认无透传。
        return {}

    # ---- 下沉的通用校验与失败文件解析 ----

    @classmethod
    def _require_library(cls, library_id: int) -> MediaLibrary:
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在", {"library_id": library_id})
        expected = cls.REQUIRED_LIBRARY_BACKEND
        if expected is not None and library.backend != expected.value:
            raise ApiError(
                422,
                "media_library_backend_mismatch",
                f"该操作要求 {expected.value} 媒体库",
                {
                    "library_id": library_id,
                    "expected_backend": expected.value,
                    "actual_backend": library.backend,
                },
            )
        return library

    @classmethod
    def _require_job(cls, job_id: int):
        job = cls.JOB_MODEL.get_or_none(cls.JOB_MODEL.id == job_id)
        if job is None:
            raise ApiError(404, cls.JOB_NOT_FOUND_CODE, cls.JOB_NOT_FOUND_MESSAGE, {cls.JOB_ID_FIELD: job_id})
        return job

    @classmethod
    def _assert_job_terminal(cls, job) -> None:
        if job.state not in TERMINAL_JOB_STATES:
            raise ApiError(
                409,
                "job_in_progress",
                "导入作业进行中，暂不能操作失败文件",
                {cls.JOB_ID_FIELD: job.id, "state": job.state},
            )

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
    def _assert_within_allowed_roots(path: str) -> None:
        assert_within_allowed_roots(Path(path), settings.media_import.browse_roots)

    @staticmethod
    def _has_live_owner(job) -> bool:
        task_run = job.task_run
        if task_run is None or task_run.owner_pid is None:
            return False
        return is_process_alive(task_run.owner_pid)

    @staticmethod
    def _resolve_source_path(source_path: str) -> Path:
        resolved = normalize_abs_path(source_path)
        assert_within_allowed_roots(resolved, settings.media_import.browse_roots)
        return resolved

    @classmethod
    def _build_mutex_key(cls, library_id: int, resolved_source: Path) -> str:
        digest = hashlib.sha1(str(resolved_source).encode("utf-8")).hexdigest()
        return f"{cls.MUTEX_PREFIX}:{library_id}:{digest}"

    @classmethod
    def _build_retry_mutex_key(cls, job_id: int, library_id: int, resolved_source: Path) -> str:
        digest = hashlib.sha1(str(resolved_source).encode("utf-8")).hexdigest()
        return f"{cls.MUTEX_PREFIX}:retry:{library_id}:{digest}:{job_id}"

    @staticmethod
    def _parse_failed_files(job) -> list[dict[str, Any]]:
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
    def _failed_file_resources(cls, job) -> list[FailedFileResource]:
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
    def _actionable_failed_paths(cls, job) -> set[str]:
        return {
            item.get("path", "")
            for item in cls._parse_failed_files(job)
            if isinstance(item, dict) and item.get("path") and cls._entry_kind(item) == FAILED_FILE_KIND_FILE
        }

    @classmethod
    def _find_failed_entry(cls, job, path: str) -> dict[str, Any] | None:
        for item in cls._parse_failed_files(job):
            if isinstance(item, dict) and item.get("path") == path:
                return item
        return None

    @classmethod
    def _require_actionable_failed_entry(cls, job, path: str) -> dict[str, Any]:
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
    def _save_failed_files(job, failure_items: list[dict[str, Any]]) -> None:
        job.failed_files = json.dumps(failure_items, ensure_ascii=False)
        job.updated_at = utc_now_for_db()
        job.save()
