"""TaskRun-only boundary for provider-owned media imports."""

from __future__ import annotations

from typing import Any

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    FAILURE_REASON_METADATA_FETCH_FAILED,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.model import BackgroundTaskRun, DownloadTask, MediaLibrary
from src.model.base import get_database
from src.plugins.provider_protocol import ProviderOperationError
from src.schema.transfers.media_import import (
    ImportAcceptedResponse,
    ImportFailedItemResource,
    ImportMetadataSearchResponse,
    ImportRequest,
)
from src.service.system import ActivityService
from src.service.transfers.downloads.common import download_provider
from src.service.transfers.shared.import_notifications import create_new_media_reminder
from src.service.transfers.shared.write_mutex import library_import_mutex_key


class ImportTaskService:
    TASK_KEY = "library_import"
    MANUAL_SEARCH_FAILURE_REASONS = frozenset(
        {
            FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
            FAILURE_REASON_METADATA_FETCH_FAILED,
        }
    )

    @classmethod
    def enqueue(
        cls,
        request: ImportRequest,
        *,
        trigger_type: str = "manual",
        download_task_id: int | None = None,
        task_name: str | None = None,
    ):
        request, library = cls._validated_request(request)
        mutex_key = library_import_mutex_key(library=library)
        params = request.model_dump()
        params["download_task_id"] = download_task_id
        try:
            download_task = (
                DownloadTask.get_by_id(download_task_id)
                if download_task_id is not None
                else None
            )
            if download_task is not None:
                # 下载任务导入只认准该任务的目标番号，资源包里的其它番号一律忽略。
                params["target_movie_number"] = download_task.movie
            with get_database().atomic():
                task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name=task_name or cls._task_name(request),
                    trigger_type=trigger_type,
                    mutex_key=mutex_key,
                    params=params,
                )
                if download_task is not None:
                    download_task.import_status = IMPORT_STATUS_RUNNING
                    download_task.import_task_run = task_run
                    download_task.save(
                        only=[DownloadTask.import_status, DownloadTask.import_task_run]
                    )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "import_task_conflict",
                "同一媒体库已有导入任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc
        except Exception as exc:
            if download_task_id is not None:
                DownloadTask.update(import_status=IMPORT_STATUS_FAILED).where(
                    DownloadTask.id == download_task_id
                ).execute()
            raise ApiError(
                502,
                "import_task_create_failed",
                "媒体导入任务入队失败",
                {"detail": str(exc)},
            ) from exc
        return ImportAcceptedResponse(
            task_run_id=task_run.id,
            task_key=task_run.task_key,
            state=task_run.state,
        )

    @classmethod
    def enqueue_batch(cls, download_tasks: list[DownloadTask]) -> None:
        if not download_tasks:
            raise ValueError("download_tasks must not be empty")
        library_ids = {task.client.library_id for task in download_tasks}
        if len(library_ids) != 1:
            raise ValueError("download_tasks must belong to one media library")
        library = MediaLibrary.get_by_id(next(iter(library_ids)))
        if library.provider_key == "":
            raise ApiError(422, "invalid_media_library_provider", "媒体库缺少 provider_key")
        batch_items = []
        for task in download_tasks:
            request = ImportRequest(
                media_kind="jav",
                library_id=library.id,
                source_ref=task.completed_source_ref,
                source_disposition="keep",
            )
            batch_items.append(
                {
                    "download_task_id": int(task.id),
                    **request.model_dump(),
                    # 下载任务导入只认准该任务的目标番号，资源包里的其它番号一律忽略。
                    "target_movie_number": task.movie,
                }
            )
        params = {
            "download_tasks": batch_items,
            "library_id": library.id,
        }
        mutex_key = library_import_mutex_key(library=library)
        try:
            with get_database().atomic():
                task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name=f"下载任务连续导入（{len(batch_items)}个）",
                    trigger_type="internal",
                    mutex_key=mutex_key,
                    params=params,
                )
                updated_count = (
                    DownloadTask.update(
                        import_status=IMPORT_STATUS_RUNNING,
                        import_task_run=task_run,
                    )
                    .where(
                        DownloadTask.id.in_([task.id for task in download_tasks]),
                        DownloadTask.import_status == IMPORT_STATUS_PENDING,
                    )
                    .execute()
                )
                if updated_count != len(download_tasks):
                    raise ApiError(
                        409,
                        "download_task_import_conflict",
                        "部分下载任务已被其它导入任务占用",
                    )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "import_task_conflict",
                "同一媒体库已有导入任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc

    @classmethod
    def list_failed_items(cls, task_run_id: int) -> list[ImportFailedItemResource]:
        task_run = cls._get_import_task_run(task_run_id)
        raw_items = (task_run.result_summary or {}).get("failed_files", [])
        return [cls._failure_item_resource(item) for item in raw_items]

    @classmethod
    def search_failed_item(
        cls,
        task_run_id: int,
        item_id: str,
        movie_number: str,
    ) -> ImportMetadataSearchResponse:
        task_run = cls._get_import_task_run(task_run_id)
        cls._ensure_retryable_task(task_run)
        item = cls._find_failure_item(task_run, item_id)
        cls._ensure_searchable_failure_item(item)
        from src.service.catalog.movie_metadata_search_service import (
            MovieMetadataSearchService,
        )

        return MovieMetadataSearchService.search_by_number(movie_number)

    @classmethod
    def enqueue_failed_item_retry(
        cls,
        task_run_id: int,
        item_id: str,
        candidate_id: str,
    ) -> ImportAcceptedResponse:
        from src.service.catalog.movie_metadata_search_service import (
            MovieMetadataSearchService,
        )

        # 入队即校验候选格式与插件启用状态，避免用户拿到一个必然失败的任务。
        MovieMetadataSearchService.resolve_candidate_reference(candidate_id)
        mutex_key = None
        try:
            with get_database().atomic():
                task_run = cls._lock_import_task_run(task_run_id)
                cls._ensure_retryable_task(task_run)
                item = cls._find_failure_item(task_run, item_id)
                cls._ensure_searchable_failure_item(item)
                source_ref = item.get("source_ref")
                if not isinstance(source_ref, dict) or not source_ref:
                    raise ApiError(409, "failed_item_source_unavailable", "失败项的源文件信息已不可用")
                library_id = item.get("library_id")
                if not isinstance(library_id, int) or isinstance(library_id, bool):
                    raise ApiError(409, "failed_item_source_unavailable", "失败项缺少媒体库信息")
                library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
                if library is None:
                    raise ApiError(404, "media_library_not_found", "媒体库不存在")
                if library.provider_key == "":
                    raise ApiError(422, "invalid_media_library_provider", "媒体库缺少 provider_key")
                mutex_key = library_import_mutex_key(library=library)
                params = {
                    "mode": "retry_failed_file",
                    "original_task_run_id": task_run.id,
                    "failure_item_id": item_id,
                    "failure_item": dict(item),
                    "candidate_id": candidate_id,
                }
                retry_task_run = ActivityService.create_task_run(
                    task_key=cls.TASK_KEY,
                    task_name="JAV失败项重试导入",
                    trigger_type="manual",
                    mutex_key=mutex_key,
                    params=params,
                )
                cls._replace_failure_item(
                    task_run,
                    item_id,
                    {
                        "state": "queued",
                        "retry_task_run_id": retry_task_run.id,
                        "last_retry_error": None,
                    },
                )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            raise ApiError(
                409,
                "import_task_conflict",
                "同一媒体库已有导入任务",
                {"blocking_task_run_id": blocking.id if blocking else None},
            ) from exc
        return ImportAcceptedResponse(
            task_run_id=retry_task_run.id,
            task_key=retry_task_run.task_key,
            state=retry_task_run.state,
        )

    @classmethod
    def execute(cls, reporter, params: dict) -> dict:
        if params.get("mode") == "retry_failed_file":
            result = cls._execute_failed_item_retry(reporter, params)
        elif "download_tasks" in params:
            result = cls._execute_batch(reporter, params)
        else:
            result = cls._execute_single(
                reporter, params, progress_callback=reporter.progress_callback
            )
        if (
            "download_tasks" in params or params.get("download_task_id") is not None
        ) and result["new_playable_movies"]:
            try:
                create_new_media_reminder(
                    movie_items=result["new_playable_movies"],
                    related_task_run_id=reporter.task_run_id,
                )
            except Exception as exc:
                logger.warning(
                    "Create import reminder skipped task_run_id={} detail={}",
                    reporter.task_run_id,
                    exc,
                )
        return result

    @classmethod
    def _execute_batch(cls, reporter, params: dict) -> dict:
        batch_items = params["download_tasks"]
        if not batch_items:
            raise ValueError("download_tasks must not be empty")
        total = len(batch_items)
        processed_count = failed_task_count = 0
        imported_count = skipped_count = failed_count = 0
        new_playable_movies = []
        created_video_ids = []
        failed_files = []
        reporter.emit(current=0, total=total, text=f"待处理下载任务 {total} 个")
        for item in batch_items:
            task_id = int(item["download_task_id"])
            try:
                result = cls._execute_single(
                    reporter,
                    item,
                    progress_callback=None,
                    operation_namespace=f"task:{reporter.task_run_id}:download:{task_id}",
                )
                imported_count += result.get("imported_count", 0)
                skipped_count += result.get("skipped_count", 0)
                failed_count += result.get("failed_count", 0)
                new_playable_movies.extend(result["new_playable_movies"])
                created_video_ids.extend(result.get("created_video_ids", []))
                failed_files.extend(result.get("failed_files", []))
                if result.get("failed_count", 0):
                    failed_task_count += 1
            except Exception:
                failed_task_count += 1
                logger.exception(
                    "Batch library import failed task_run_id={} download_task_id={}",
                    reporter.task_run_id,
                    task_id,
                )
            finally:
                processed_count += 1
                reporter.emit(
                    current=processed_count,
                    total=total,
                    text=f"已处理下载任务 {processed_count}/{total}",
                )
        return {
            "download_task_count": total,
            "processed_download_task_count": processed_count,
            "failed_download_task_count": failed_task_count,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "new_playable_movies": new_playable_movies,
            "created_video_ids": created_video_ids,
            "failed_files": failed_files,
        }

    @classmethod
    def _execute_failed_item_retry(cls, reporter, params: dict) -> dict:
        original_task_run_id = int(params["original_task_run_id"])
        failure_item_id = str(params["failure_item_id"])
        candidate_id = str(params["candidate_id"])
        reporter.emit(current=0, total=1, text="正在重试失败视频")
        try:
            from src.service.transfers.imports.import_service import MediaImportService

            result = MediaImportService().retry_failed_file(
                params["failure_item"],
                candidate_id,
                operation_key=f"task:{reporter.task_run_id}:retry",
            )
        except Exception as exc:
            try:
                cls._update_failure_item(
                    original_task_run_id,
                    failure_item_id,
                    state="pending",
                    retry_task_run_id=reporter.task_run_id,
                    last_retry_error=str(exc),
                )
            except Exception:
                logger.exception(
                    "Failed to persist failed-item retry error original_task_run_id={} item_id={}",
                    original_task_run_id,
                    failure_item_id,
                )
            raise

        cls._update_failure_item(
            original_task_run_id,
            failure_item_id,
            state="resolved",
            retry_task_run_id=reporter.task_run_id,
            resolved_movie_id=result["movie_id"],
            resolved_media_id=result["media_id"],
            last_retry_error=None,
        )
        reporter.emit(
            current=1,
            total=1,
            text="失败视频重试完成",
            summary_patch={"imported_count": 1, "failed_count": 0},
        )
        return {
            **result,
            "original_task_run_id": original_task_run_id,
            "failure_item_id": failure_item_id,
            "candidate_id": candidate_id,
        }

    @classmethod
    def _execute_single(
        cls,
        reporter,
        params: dict,
        *,
        progress_callback,
        operation_namespace: str | None = None,
    ) -> dict:
        request = ImportRequest.model_validate(params)
        download_task_id = params.get("download_task_id")
        task_run_id = getattr(reporter, "task_run_id", None)
        if not isinstance(task_run_id, int):
            raise TypeError("import_task_run_id_missing")
        target_movie_number = params.get("target_movie_number")
        try:
            from src.service.transfers.imports.import_service import MediaImportService

            result = MediaImportService().import_from_source(
                request.source_ref,
                request.library_id,
                media_kind=request.media_kind,
                source_disposition=request.source_disposition,
                collection_id=request.collection_id,
                progress_callback=progress_callback,
                operation_namespace=operation_namespace or f"task:{task_run_id}",
                target_movie_number=target_movie_number,
            )
        except Exception:
            cls._set_download_status(download_task_id, IMPORT_STATUS_FAILED)
            raise
        if result.failed_count:
            status = IMPORT_STATUS_FAILED
        elif result.imported_count:
            status = IMPORT_STATUS_COMPLETED
        else:
            status = IMPORT_STATUS_SKIPPED
        cls._set_download_status(download_task_id, status)
        if status == IMPORT_STATUS_COMPLETED:
            cls._delete_remote_download_task(download_task_id)
        summary = result.model_dump()
        if download_task_id is not None:
            summary["download_task_id"] = int(download_task_id)
        return summary

    @classmethod
    def _get_import_task_run(cls, task_run_id: int) -> BackgroundTaskRun:
        task_run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
        if task_run is None or task_run.task_key != cls.TASK_KEY:
            raise ApiError(404, "import_task_not_found", "导入任务不存在")
        return task_run

    @classmethod
    def _lock_import_task_run(cls, task_run_id: int) -> BackgroundTaskRun:
        task_run = (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.id == task_run_id)
            .for_update()
            .get_or_none()
        )
        if task_run is None or task_run.task_key != cls.TASK_KEY:
            raise ApiError(404, "import_task_not_found", "导入任务不存在")
        return task_run

    @classmethod
    def _ensure_retryable_task(cls, task_run: BackgroundTaskRun) -> None:
        if task_run.state not in {"completed", "failed"}:
            raise ApiError(409, "import_task_not_finished", "导入任务尚未完成")

    @classmethod
    def _find_failure_item(cls, task_run: BackgroundTaskRun, item_id: str) -> dict[str, Any]:
        for item in (task_run.result_summary or {}).get("failed_files", []):
            if item.get("id") == item_id:
                return item
        raise ApiError(404, "failed_item_not_found", "导入失败项不存在")

    @classmethod
    def _ensure_searchable_failure_item(cls, item: dict[str, Any]) -> None:
        if item.get("state", "pending") != "pending":
            raise ApiError(409, "failed_item_not_pending", "失败项当前不在待处理状态")
        if (
            item.get("media_kind") != "jav"
            or item.get("is_video") is not True
            or item.get("reason") not in cls.MANUAL_SEARCH_FAILURE_REASONS
        ):
            raise ApiError(409, "failed_item_search_unavailable", "该失败项不支持手动元数据搜索")

    @classmethod
    def _failure_item_resource(cls, item: dict[str, Any]) -> ImportFailedItemResource:
        state = item["state"]
        return ImportFailedItemResource(
            id=item["id"],
            relative_path=item["relative_path"],
            size_bytes=item["size_bytes"],
            is_video=item["is_video"],
            reason=item["reason"],
            detail=item["detail"],
            kind=item["kind"],
            state=state,
            retry_task_run_id=item["retry_task_run_id"],
            resolved_movie_id=item["resolved_movie_id"],
            resolved_media_id=item["resolved_media_id"],
            last_retry_error=item["last_retry_error"],
            can_manual_search=(
                state == "pending"
                and item["is_video"]
                and item["media_kind"] == "jav"
                and item["reason"] in cls.MANUAL_SEARCH_FAILURE_REASONS
            ),
        )

    @staticmethod
    def _replace_failure_item(
        task_run: BackgroundTaskRun,
        item_id: str,
        changes: dict[str, Any],
    ) -> None:
        """在已锁定的任务行上替换失败项并保存；找不到即 404。"""
        raw_items = (task_run.result_summary or {}).get("failed_files", [])
        updated_items = []
        replaced = False
        for item in raw_items:
            if item.get("id") == item_id:
                updated_items.append({**item, **changes})
                replaced = True
            else:
                updated_items.append(item)
        if not replaced:
            raise ApiError(404, "failed_item_not_found", "导入失败项不存在")
        summary = dict(task_run.result_summary or {})
        summary["failed_files"] = updated_items
        task_run.result_summary = summary
        task_run.save(only=[BackgroundTaskRun.result_summary])

    @classmethod
    def _update_failure_item(cls, task_run_id: int, item_id: str, **changes) -> None:
        with get_database().atomic():
            task_run = cls._lock_import_task_run(task_run_id)
            cls._replace_failure_item(task_run, item_id, changes)

    @staticmethod
    def _validated_request(request: ImportRequest) -> tuple[ImportRequest, MediaLibrary]:
        library = MediaLibrary.get_or_none(MediaLibrary.id == request.library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在")
        if library.provider_key == "":
            raise ApiError(422, "invalid_media_library_provider", "媒体库缺少 provider_key")
        return request, library

    @staticmethod
    def _task_name(request: ImportRequest) -> str:
        kind = "JAV" if request.media_kind == "jav" else "视频"
        return f"{kind}媒体库导入"

    @staticmethod
    def _set_download_status(download_task_id: int | None, status: str) -> None:
        if download_task_id is None:
            return
        DownloadTask.update(import_status=status).where(
            DownloadTask.id == int(download_task_id)
        ).execute()

    @staticmethod
    def _delete_remote_download_task(download_task_id: int | None) -> None:
        """Remove the provider task record after a successful import, preserving files."""
        if download_task_id is None:
            return
        task = DownloadTask.get_or_none(DownloadTask.id == int(download_task_id))
        if task is None:
            return
        try:
            download_provider(task.client).delete_task(
                remote_id=task.remote_id,
                delete_files=False,
            )
        except ProviderOperationError as exc:
            if exc.code == "source_not_found":
                return
            logger.warning(
                "Auto-delete remote download task failed task_id={} provider={} operation={} code={}",
                task.id,
                exc.provider_key,
                exc.operation,
                exc.code,
            )
        except ApiError as exc:
            logger.warning(
                "Auto-delete remote download task unavailable task_id={} code={}",
                task.id,
                exc.code,
            )
        except Exception as exc:
            logger.warning(
                "Auto-delete remote download task failed unexpectedly task_id={} error_type={}",
                task.id,
                type(exc).__name__,
            )

    @classmethod
    def recover_interrupted_downloads(cls) -> int:
        failed_runs = BackgroundTaskRun.select(BackgroundTaskRun.id).where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        )
        recovered = DownloadTask.update(import_status=IMPORT_STATUS_PENDING).where(
            (DownloadTask.import_status == IMPORT_STATUS_RUNNING)
            & DownloadTask.import_task_run.in_(failed_runs)
        ).execute()
        restored = cls._restore_interrupted_failure_item_retries()
        if restored:
            logger.info("Restored interrupted failed-item retries count={}", restored)
        return recovered

    @classmethod
    def _restore_interrupted_failure_item_retries(cls) -> int:
        """重试任务被中断时失败项会停在 queued；只能靠 failed 运行的参数把状态放回 pending。"""
        restored = 0
        for task_run in BackgroundTaskRun.select(
            BackgroundTaskRun.id,
            BackgroundTaskRun.params,
            BackgroundTaskRun.error_message,
        ).where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        ):
            params = task_run.params or {}
            if params.get("mode") != "retry_failed_file":
                continue
            restored += cls._restore_failure_item(
                params.get("original_task_run_id"),
                params.get("failure_item_id"),
                task_run.id,
                task_run.error_message,
            )
        return restored

    @classmethod
    def _restore_failure_item(
        cls,
        original_task_run_id,
        item_id,
        retry_task_run_id: int,
        error_message: str | None,
    ) -> int:
        if (
            not isinstance(original_task_run_id, int)
            or isinstance(original_task_run_id, bool)
            or not isinstance(item_id, str)
            or not item_id
        ):
            return 0
        with get_database().atomic():
            task_run = (
                BackgroundTaskRun.select()
                .where(BackgroundTaskRun.id == original_task_run_id)
                .for_update()
                .get_or_none()
            )
            if task_run is None or task_run.task_key != cls.TASK_KEY:
                return 0
            item = next(
                (
                    current
                    for current in (task_run.result_summary or {}).get(
                        "failed_files", []
                    )
                    if current.get("id") == item_id
                ),
                None,
            )
            if (
                item is None
                or item.get("state") != "queued"
                or item.get("retry_task_run_id") != retry_task_run_id
            ):
                return 0
            cls._replace_failure_item(
                task_run,
                item_id,
                {
                    "state": "pending",
                    "last_retry_error": item.get("last_retry_error")
                    or error_message
                    or "重试任务中断",
                },
            )
            return 1
