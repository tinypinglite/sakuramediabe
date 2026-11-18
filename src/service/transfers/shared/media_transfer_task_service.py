"""Move an existing Media; interrupted work never resumes destructive provider calls."""

import time
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any

from loguru import logger
from peewee import DatabaseError

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import BackgroundTaskRun, Media, MediaLibrary, get_database
from src.plugins.operation_lock import plugin_operation_lock
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ImportPlacement,
    MediaTransferSourceInfo,
    ProviderOperationError,
    ProviderUnavailableError,
    StagedMediaTransfer,
    supports_media_transfer_source,
    supports_media_transfer_source_cleanup,
    supports_media_transfer_target,
)
from src.schema.transfers.media_transfer import (
    MediaStorageTransferAcceptedResponse,
    MediaStorageTransferCandidatesRequest,
    MediaStorageTransferCandidatesResponse,
    MediaStorageTransferFileResource,
    MediaStorageTransferLibraryResource,
    MediaStorageTransferRequest,
    MediaStorageTransferResult,
)
from src.service.playback.operation_locks import (
    LIBRARY_LOCK,
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.provider_helpers import library_handle_for, media_handle_for
from src.service.system import ActivityService
from src.service.transfers.shared.write_scope import library_import_scope_key


class MediaTransferTaskService:
    TASK_KEY = "media_storage_transfer"
    STAGE_TRANSFER_ATTEMPTS = 3
    STAGE_TRANSFER_RETRY_DELAYS = (0.5, 1.0)
    BATCH_FATAL_REASONS = frozenset(
        {
            "media_operation_connection_lost",
            "media_transfer_not_running",
            "media_transfer_cannot_resume",
            "media_transfer_wrong_item",
            "media_transfer_wrong_task",
            "媒体库配置已变化，请重新发起",
        }
    )

    @classmethod
    def list_candidates(
        cls, request: MediaStorageTransferCandidatesRequest
    ) -> MediaStorageTransferCandidatesResponse:
        _, source_library = cls._validated_source(request.media_ids)
        source_storage = cls._storage_for(source_library)
        cls._require_source_capability(source_storage)
        targets = []
        for target_library in MediaLibrary.select().order_by(
            MediaLibrary.created_at.desc(), MediaLibrary.id.desc()
        ):
            if target_library.id == source_library.id:
                continue
            try:
                target_storage = cls._storage_for(target_library)
            except ApiError:
                continue
            if supports_media_transfer_target(target_storage):
                targets.append(
                    MediaStorageTransferLibraryResource(
                        id=target_library.id,
                        name=target_library.name,
                    )
                )
        return MediaStorageTransferCandidatesResponse(
            source_library=MediaStorageTransferLibraryResource(
                id=source_library.id,
                name=source_library.name,
            ),
            targets=targets,
        )

    @classmethod
    def enqueue(
        cls, request: MediaStorageTransferRequest
    ) -> MediaStorageTransferAcceptedResponse:
        # Old copy jobs/receipts must be handled explicitly, never interpreted as moves.
        for run in BackgroundTaskRun.select().where(
            BackgroundTaskRun.task_key == cls.TASK_KEY
        ):
            params = run.params or {}
            if params.get("_staged_transfers") or (
                run.state in {"pending", "running"}
                and "_library_versions" not in params
            ):
                raise ApiError(
                    409,
                    "media_transfer_legacy_task",
                    "请先处理旧媒体复制任务",
                    {"task_run_id": run.id},
                )
        medias, source, target = cls._validated_request(request)
        cls._require_capabilities(cls._storage_for(source), cls._storage_for(target))
        params = request.model_dump()
        params.update(
            _source_library_id=source.id,
            _library_versions={
                str(lib.id): lib.updated_at.isoformat() for lib in (source, target)
            },
            _move_index=0,
        )
        files = [
            MediaStorageTransferFileResource(
                media_id=media.id,
                file_name=media.file_name,
                movie_number=media.movie_number,
                size_bytes=media.file_size_bytes,
            )
            for media in medias
        ]
        summary = MediaStorageTransferResult(
            unexecuted_media_ids=[m.id for m in medias],
            files=files,
        ).model_dump()
        with get_database().atomic():
            task = ActivityService.create_task_run(
                task_key=cls.TASK_KEY,
                task_name="媒体存储迁移",
                trigger_type="manual",
                scope_key=library_import_scope_key(library=target),
                params=params,
            )
            task.result_summary = summary
            task.save(only=[BackgroundTaskRun.result_summary])
        return MediaStorageTransferAcceptedResponse(
            task_run_id=task.id, task_key=task.task_key, state=task.state
        )

    @classmethod
    def list_files(
        cls, task_run_id: int
    ) -> list[MediaStorageTransferFileResource]:
        run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
        if run is None or run.task_key != cls.TASK_KEY:
            raise ApiError(404, "media_transfer_task_not_found", "迁移任务不存在")
        raw_files = (run.result_summary or {}).get("files", [])
        if not isinstance(raw_files, list):
            return []
        return [
            MediaStorageTransferFileResource.model_validate(item)
            for item in raw_files
        ]

    @staticmethod
    def _require_source_capability(source):
        if not supports_media_transfer_source(
            source
        ) or not supports_media_transfer_source_cleanup(source):
            raise ApiError(
                422, "media_transfer_source_unsupported", "源媒体库不支持迁移"
            )

    @classmethod
    def _require_capabilities(cls, source, target):
        cls._require_source_capability(source)
        if not supports_media_transfer_target(target):
            raise ApiError(
                422, "media_transfer_target_unsupported", "目标媒体库不支持迁移"
            )

    @classmethod
    def _stage_transfer_with_retry(
        cls,
        *,
        task_id: int,
        media_id: int,
        index: int,
        source,
        target_storage,
        placement: ImportPlacement,
    ) -> StagedMediaTransfer:
        operation_key = f"task:{task_id}:{index + 1}"
        for attempt in range(1, cls.STAGE_TRANSFER_ATTEMPTS + 1):
            try:
                return target_storage.stage_transfer(
                    source=source,
                    placement=placement,
                    operation_key=operation_key,
                )
            except ProviderOperationError as exc:
                if (
                    exc.code != "unavailable"
                    or not exc.retryable
                    or attempt >= cls.STAGE_TRANSFER_ATTEMPTS
                ):
                    raise
                delay = cls.STAGE_TRANSFER_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "Media storage transfer stage retry task_run_id={} media_id={} attempt={}/{} delay_seconds={} error_code={}",
                    task_id,
                    media_id,
                    attempt,
                    cls.STAGE_TRANSFER_ATTEMPTS,
                    delay,
                    exc.code,
                )
                time.sleep(delay)
        raise AssertionError("unreachable stage transfer retry state")

    @classmethod
    def _is_batch_fatal(cls, exc: Exception) -> bool:
        if isinstance(exc, ProviderUnavailableError | DatabaseError):
            return True
        if isinstance(exc, ProviderOperationError):
            return exc.code == "authentication_failed"
        if isinstance(exc, ApiError) and exc.status_code != 409:
            return True
        return isinstance(exc, RuntimeError) and str(exc) in cls.BATCH_FATAL_REASONS

    @classmethod
    def execute(cls, reporter, params: dict[str, Any]) -> dict[str, Any]:
        request = MediaStorageTransferRequest.model_validate(params)
        task_id = reporter.task_run_id
        if (
            not params.get("_library_versions")
            or params.get("_staged_transfers")
            or params.get("_move_index", 0)
            or params.get("_current_item")
        ):
            raise RuntimeError("旧任务或中断任务不能作为新迁移执行")
        reporter.summary = dict(
            BackgroundTaskRun.get_by_id(task_id).result_summary or {}
        )
        reason = "transfer_setup_failed"
        try:
            with ExitStack() as batch:
                batch.enter_context(
                    plugin_operation_lock(Path(settings.plugins.root_dir), shared=True)
                )
                source_id = params["_source_library_id"]
                for library_id in sorted({source_id, request.target_library_id}):
                    check_connection = batch.enter_context(
                        media_operation_lock(LIBRARY_LOCK, library_id)
                    )
                source_library = MediaLibrary.get_by_id(source_id)
                target_library = MediaLibrary.get_by_id(request.target_library_id)
                reason = "library_changed"
                for lib in (source_library, target_library):
                    if (
                        params["_library_versions"].get(str(lib.id))
                        != lib.updated_at.isoformat()
                    ):
                        raise RuntimeError("媒体库配置已变化，请重新发起")
                source_storage, target_storage = (
                    cls._storage_for(source_library),
                    cls._storage_for(target_library),
                )
                cls._require_capabilities(source_storage, target_storage)
                logger.info(
                    "Media storage transfer started source_library_id={} target_library_id={} media_count={}",
                    source_id,
                    request.target_library_id,
                    len(request.media_ids),
                )
                for index, media_id in enumerate(request.media_ids):
                    reason = "media_unavailable"
                    with ExitStack() as item_scope:
                        try:
                            item_scope.enter_context(
                                media_operation_lock(MEDIA_LOCK, media_id)
                            )
                        except MediaOperationBusy:
                            # 单媒体被其他任务占用只跳过当前条目，不能终止整批。
                            cls._skip_busy_item(task_id, reporter, index, media_id)
                            continue
                        check_connection()
                        cls._begin_item(task_id, index, media_id)
                        media = Media.get_or_none(Media.id == media_id)
                        if (
                            media is None
                            or not media.valid
                            or media.library_id != source_id
                        ):
                            cls._record_failure(
                                task_id,
                                "media_unavailable",
                                reporter=reporter,
                                terminal=False,
                            )
                            continue
                        original = media_handle_for(media)
                        staged = None
                        switch_attempted = False
                        try:
                            with source_storage.open_transfer_source(
                                media=original
                            ) as source:
                                cls._validate_source(
                                    source,
                                    expected_file_name=media.file_name,
                                    expected_size_bytes=media.file_size_bytes,
                                )
                                cls._persist_source_hashes(media, source.info)
                                reason = "stage_failed"
                                staged = cls._stage_transfer_with_retry(
                                    task_id=task_id,
                                    media_id=media_id,
                                    index=index,
                                    source=source,
                                    target_storage=target_storage,
                                    placement=ImportPlacement(
                                        relative_path=cls._placement_for(media)
                                    ),
                                )
                                check_connection()
                                cls._require_running(task_id)
                                if not isinstance(staged, StagedMediaTransfer):
                                    raise TypeError("invalid transfer response")
                                if staged.status == "not_available":
                                    cls._validate_not_available(staged)
                                    cls._finish_item(task_id, reporter, "skipped_count")
                                    continue
                                cls._validate_staged(
                                    staged, source_size_bytes=source.info.size_bytes
                                )
                                if staged.file_name != original.file_name:
                                    raise ValueError("target name changed")
                                reason = "source_changed"
                                source.assert_unchanged()
                                check_connection()
                                reason = "switch_failed"
                                # Attempted, not committed: commit ACK loss must never authorize abort.
                                switch_attempted = True
                                cls._switch_media(
                                    task_id, media, target_library, staged
                                )
                                reason = "target_verification_failed"
                                target_storage.finalize_transfer(receipt=staged.receipt)
                                check_connection()
                                cls._require_running(task_id)
                                reason = "source_cleanup_failed"
                                source_storage.cleanup_transfer_source(
                                    media=original, source=source
                                )
                                reason = "cleanup_unconfirmed"
                                check_connection()
                                cls._finish_item(task_id, reporter, "transferred_count")
                        except Exception as exc:
                            if (
                                not switch_attempted
                                and isinstance(staged, StagedMediaTransfer)
                                and staged.status == "staged"
                                and staged.receipt
                            ):
                                try:
                                    check_connection()
                                    cls._require_running(task_id)
                                    target_storage.abort_transfer(
                                        receipt=staged.receipt
                                    )
                                except Exception:
                                    logger.warning(
                                        "媒体迁移补偿未完成 task_run_id={} media_id={}",
                                        task_id,
                                        media_id,
                                    )
                            if cls._is_batch_fatal(exc):
                                raise
                            cls._record_failure(
                                task_id, reason, reporter=reporter, terminal=False
                            )
                            continue
                summary = dict(reporter.summary or {})
                if (
                    summary.get("transferred_count", 0) == 0
                    and summary.get("skipped_count", 0) == 0
                    and (
                        summary.get("failed_count", 0)
                        or summary.get("cleanup_incomplete_count", 0)
                    )
                ):
                    raise RuntimeError("媒体迁移未全部完成，请查看任务结果")
            reporter.summary = BackgroundTaskRun.get_by_id(task_id).result_summary
            return dict(reporter.summary)
        except Exception as exc:
            if isinstance(exc, ApiError) and exc.status_code == 409:
                reason = "media_busy"
            # 只记 reason，不落 provider 异常细节，避免凭据进入日志。
            logger.warning(
                "Media storage transfer failed task_run_id={} reason={}",
                task_id,
                reason,
            )
            try:
                cls._record_failure(task_id, reason, reporter=reporter)
                reporter.summary = BackgroundTaskRun.get_by_id(task_id).result_summary
            except Exception:
                logger.warning("媒体迁移结果未确认 task_run_id={}", task_id)
            # Do not expose provider exceptions, credentials or frame locals to activity logs.
            raise RuntimeError("媒体迁移未全部完成，请查看任务结果") from None

    @classmethod
    def _require_running(cls, task_id):
        run = BackgroundTaskRun.get_by_id(task_id)
        if run.task_key != cls.TASK_KEY or run.state != "running":
            raise RuntimeError("media_transfer_not_running")
        return run

    @classmethod
    def _locked_run(cls, task_id):
        run = (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.id == task_id)
            .for_update()
            .get()
        )
        if run.task_key != cls.TASK_KEY:
            raise RuntimeError("media_transfer_wrong_task")
        return run

    @classmethod
    def _begin_item(cls, task_id, index, media_id):
        with get_database().atomic():
            run = cls._locked_run(task_id)
            cls._require_running(task_id)
            params = dict(run.params)
            if params.get("_move_index", 0) != index or params.get("_current_item"):
                raise RuntimeError("media_transfer_cannot_resume")
            params["_current_item"] = {"media_id": media_id, "phase": "processing"}
            run.params = params
            run.save(only=[BackgroundTaskRun.params])

    @classmethod
    def _switch_media(cls, task_id, original, target_library, staged):
        with get_database().atomic():
            current = Media.select().where(Media.id == original.id).for_update().get()
            fields = (
                "library_id",
                "storage_ref",
                "file_name",
                "file_size_bytes",
                "valid",
            )
            if any(
                getattr(current, name) != getattr(original, name) for name in fields
            ):
                raise RuntimeError("media_transfer_source_changed")
            run = cls._locked_run(task_id)
            cls._require_running(task_id)
            params = deepcopy(run.params)
            if params.get("_current_item") != {
                "media_id": original.id,
                "phase": "processing",
            }:
                raise RuntimeError("media_transfer_wrong_item")
            Media.update(
                library=target_library.id,
                storage_ref=staged.storage_ref,
                file_name=staged.file_name,
                file_size_bytes=staged.size_bytes,
                import_source_identity=None,
                updated_at=utc_now_for_db(),
            ).where(Media.id == original.id).execute()
            params["_current_item"]["phase"] = "media_switched"
            run.params = params
            run.save(only=[BackgroundTaskRun.params])

    @classmethod
    def _finish_item(cls, task_id, reporter, count_key):
        with get_database().atomic():
            run = cls._locked_run(task_id)
            cls._require_running(task_id)
            params = dict(run.params)
            index = params["_move_index"]
            params.pop("_current_item")
            params["_move_index"] += 1
            summary = dict(run.result_summary)
            summary[count_key] += 1
            if count_key == "transferred_count":
                cls._update_file_result(
                    summary,
                    index,
                    status="transferred",
                    target_committed=True,
                )
            else:
                cls._update_file_result(
                    summary,
                    index,
                    status="skipped",
                    reason_code="target_not_available",
                )
            summary["unexecuted_media_ids"] = params["media_ids"][
                params["_move_index"] :
            ]
            run.params, run.result_summary = params, summary
            run.progress_current, run.progress_total = (
                params["_move_index"],
                len(params["media_ids"]),
            )
            run.save(
                only=[
                    BackgroundTaskRun.params,
                    BackgroundTaskRun.result_summary,
                    BackgroundTaskRun.progress_current,
                    BackgroundTaskRun.progress_total,
                ]
            )
        reporter.summary = summary

    @classmethod
    def _skip_busy_item(cls, task_id, reporter, index, media_id):
        """媒体被其他任务占用：记为失败并推进游标，避免单条冲突终止整批。"""
        with get_database().atomic():
            run = cls._locked_run(task_id)
            cls._require_running(task_id)
            params = dict(run.params)
            if params.get("_move_index", 0) != index or params.get("_current_item"):
                raise RuntimeError("media_transfer_cannot_resume")
            params["_move_index"] = index + 1
            summary = dict(run.result_summary)
            summary["failed_count"] = summary.get("failed_count", 0) + 1
            summary["issues"] = [
                *summary.get("issues", []),
                {
                    "media_id": media_id,
                    "reason_code": "media_busy",
                    "target_committed": False,
                },
            ]
            cls._update_file_result(
                summary,
                index,
                status="failed",
                reason_code="media_busy",
            )
            summary["unexecuted_media_ids"] = params["media_ids"][index + 1 :]
            summary["reason_code"] = "media_busy"
            run.params, run.result_summary = params, summary
            run.progress_current, run.progress_total = (
                index + 1,
                len(params["media_ids"]),
            )
            run.save(
                only=[
                    BackgroundTaskRun.params,
                    BackgroundTaskRun.result_summary,
                    BackgroundTaskRun.progress_current,
                    BackgroundTaskRun.progress_total,
                ]
            )
        reporter.summary = summary
        logger.info(
            "Media storage transfer skipped busy media task_run_id={} media_id={}",
            task_id,
            media_id,
        )

    @classmethod
    def _record_failure(cls, task_id, reason, *, reporter=None, terminal=True):
        run = BackgroundTaskRun.get_by_id(task_id)
        item = (run.params or {}).get("_current_item")
        with ExitStack() as stack:
            if item:
                stack.enter_context(media_operation_lock(MEDIA_LOCK, item["media_id"]))
            with get_database().atomic():
                run = cls._locked_run(task_id)
                params = dict(run.params or {})
                if "_library_versions" not in params or run.state == "completed":
                    return
                summary = dict(run.result_summary or {})
                item = params.pop("_current_item", None)
                index = params.get("_move_index", 0)
                if item:
                    switched = item["phase"] == "media_switched"
                    key = "cleanup_incomplete_count" if switched else "failed_count"
                    summary[key] = summary.get(key, 0) + 1
                    summary["issues"] = [
                        *summary.get("issues", []),
                        {
                            "media_id": item["media_id"],
                            "reason_code": reason,
                            "target_committed": switched,
                        },
                    ]
                    cls._update_file_result(
                        summary,
                        index,
                        status="cleanup_incomplete" if switched else "failed",
                        reason_code=reason,
                        target_committed=switched,
                    )
                    index += 1
                    params["_move_index"] = index
                summary["unexecuted_media_ids"] = params["media_ids"][index:]
                if terminal:
                    cls._mark_remaining_unexecuted(summary, index)
                summary["reason_code"] = reason
                run.params, run.result_summary = params, summary
                run.progress_current, run.progress_total = (
                    index,
                    len(params["media_ids"]),
                )
                run.save(
                    only=[
                        BackgroundTaskRun.params,
                        BackgroundTaskRun.result_summary,
                        BackgroundTaskRun.progress_current,
                        BackgroundTaskRun.progress_total,
                    ]
                )
        if reporter is not None:
            reporter.summary = summary

    @staticmethod
    def _update_file_result(
        summary: dict[str, Any],
        index: int,
        *,
        status: str,
        reason_code: str | None = None,
        target_committed: bool = False,
    ) -> None:
        raw_files = summary.get("files")
        if not isinstance(raw_files, list) or index < 0 or index >= len(raw_files):
            return
        files = [dict(item) for item in raw_files]
        files[index].update(
            {
                "status": status,
                "reason_code": reason_code,
                "target_committed": target_committed,
            }
        )
        summary["files"] = files

    @classmethod
    def _mark_remaining_unexecuted(
        cls, summary: dict[str, Any], start_index: int
    ) -> None:
        raw_files = summary.get("files")
        if not isinstance(raw_files, list):
            return
        files = [dict(item) for item in raw_files]
        for item in files[start_index:]:
            if item.get("status") == "pending":
                item.update(
                    {
                        "status": "unexecuted",
                        "reason_code": None,
                        "target_committed": False,
                    }
                )
        summary["files"] = files

    @classmethod
    def recover_interrupted_transfers(cls) -> dict[str, int]:
        count = 0
        for run in BackgroundTaskRun.select().where(
            (BackgroundTaskRun.task_key == cls.TASK_KEY)
            & (BackgroundTaskRun.state == "failed")
        ):
            params = run.params or {}
            if "_library_versions" not in params or (
                not params.get("_current_item")
                and run.result_summary.get("reason_code")
            ):
                continue
            try:
                cls._record_failure(
                    run.id,
                    "cleanup_unconfirmed"
                    if params.get("_current_item", {}).get("phase") == "media_switched"
                    else "interrupted",
                )
                count += 1
            except MediaOperationBusy:
                continue
        return {"interrupted_runs": count}

    @classmethod
    def _validated_request(
        cls,
        request: MediaStorageTransferRequest,
    ) -> tuple[list[Media], MediaLibrary, MediaLibrary]:
        medias, source_library = cls._validated_source(request.media_ids)
        target_library = MediaLibrary.get_or_none(
            MediaLibrary.id == request.target_library_id
        )
        if target_library is None:
            raise ApiError(
                404, "media_transfer_target_library_not_found", "目标媒体库不存在"
            )
        if target_library.id == source_library.id:
            raise ApiError(422, "media_transfer_same_library", "源和目标媒体库不能相同")
        return medias, source_library, target_library

    @staticmethod
    def _validated_source(media_ids: list[int]) -> tuple[list[Media], MediaLibrary]:
        source_rows = {
            media.id: media
            for media in Media.select().where(Media.id.in_(media_ids))
        }
        if len(source_rows) != len(media_ids):
            raise ApiError(404, "media_transfer_source_not_found", "部分源媒体不存在")
        medias = [source_rows[media_id] for media_id in media_ids]
        if any(not media.valid for media in medias):
            raise ApiError(422, "media_transfer_source_invalid", "源媒体包含无效文件")
        source_library_ids = {media.library_id for media in medias}
        if len(source_library_ids) != 1:
            raise ApiError(
                422,
                "media_transfer_source_library_mismatch",
                "源媒体必须属于同一媒体库",
            )
        source_library = MediaLibrary.get_by_id(next(iter(source_library_ids)))
        return medias, source_library

    @staticmethod
    def _storage_for(library: MediaLibrary):
        try:
            return MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
        except ProviderUnavailableError as exc:
            raise ApiError(503, "provider_not_installed", "媒体提供方未安装") from exc
        except ProviderOperationError as exc:
            status = {
                "invalid_config": 422,
                "authentication_failed": 401,
                "source_not_found": 404,
                "unsupported": 422,
                "unavailable": 503,
            }.get(exc.code, 502)
            raise ApiError(status, f"provider_{exc.code}", exc.safe_message) from exc

    @staticmethod
    def _placement_for(media: Media) -> str:
        file_name = media.file_name
        if not isinstance(file_name, str) or not file_name or file_name in {".", ".."}:
            raise RuntimeError("media_transfer_source_file_name_invalid")
        if any(marker in file_name for marker in ("/", "\\", "\x00")):
            raise RuntimeError("media_transfer_source_file_name_invalid")
        if media.movie_number is not None:
            return f"jav/{media.movie_number}/{file_name}"
        return f"videos/{file_name}"

    @staticmethod
    def _validate_not_available(staged: StagedMediaTransfer) -> None:
        if any(
            value is not None
            for value in (
                staged.storage_ref,
                staged.receipt,
                staged.file_name,
                staged.size_bytes,
            )
        ):
            raise RuntimeError("media_transfer_provider_invalid_not_available")

    @staticmethod
    def _validate_source(
        source: object,
        *,
        expected_file_name: str,
        expected_size_bytes: int,
    ) -> None:
        info = getattr(source, "info", None)
        if not isinstance(info, MediaTransferSourceInfo):
            raise TypeError("media_transfer_source_invalid_info")
        if (
            info.file_name != expected_file_name
            or not isinstance(info.size_bytes, int)
            or isinstance(info.size_bytes, bool)
            or info.size_bytes < 0
            or info.size_bytes != expected_size_bytes
        ):
            raise RuntimeError("media_transfer_source_snapshot_mismatch")
        if not callable(getattr(source, "open_reader", None)) or not callable(
            getattr(source, "assert_unchanged", None)
        ):
            raise TypeError("media_transfer_source_invalid_session")
        content_md5 = info.content_md5
        content_sha1 = info.content_sha1
        content_gcid = info.content_gcid
        if content_md5 is not None and not MediaTransferTaskService._valid_hex(
            content_md5, 32
        ):
            raise TypeError("media_transfer_source_invalid_hashes")
        if content_sha1 is not None and not MediaTransferTaskService._valid_hex(
            content_sha1, 40
        ):
            raise TypeError("media_transfer_source_invalid_hashes")
        if content_gcid is not None and not MediaTransferTaskService._valid_hex(
            content_gcid, 40
        ):
            raise TypeError("media_transfer_source_invalid_hashes")

    @staticmethod
    def _valid_hex(value: object, length: int) -> bool:
        return (
            isinstance(value, str)
            and len(value) == length
            and all(character in "0123456789abcdefABCDEF" for character in value)
        )

    @staticmethod
    def _persist_source_hashes(media: Media, info: MediaTransferSourceInfo) -> None:
        fields = {}
        if info.content_md5 is not None:
            fields["content_md5"] = info.content_md5.lower()
        if info.content_sha1 is not None:
            fields["content_sha1"] = info.content_sha1.lower()
        if info.content_gcid is not None:
            fields["content_gcid"] = info.content_gcid.upper()
        if not fields:
            return
        if all(getattr(media, name) == value for name, value in fields.items()):
            return
        for name, value in fields.items():
            setattr(media, name, value)
        # 哈希缓存不是用户可见的媒体修改，不推进 updated_at 改变列表排序。
        Media.update(**fields).where(Media.id == media.id).execute()

    @staticmethod
    def _validate_staged(
        staged: StagedMediaTransfer, *, source_size_bytes: int
    ) -> None:
        if staged.status != "staged":
            raise RuntimeError("media_transfer_provider_invalid_status")
        if not isinstance(staged.storage_ref, dict) or not staged.storage_ref:
            raise RuntimeError("media_transfer_provider_missing_storage_ref")
        if not isinstance(staged.receipt, dict) or not staged.receipt:
            raise RuntimeError("media_transfer_provider_missing_receipt")
        if (
            not isinstance(staged.file_name, str)
            or not staged.file_name
            or staged.file_name in {".", ".."}
            or any(marker in staged.file_name for marker in ("/", "\\", "\x00"))
        ):
            raise RuntimeError("media_transfer_provider_invalid_file_name")
        if (
            not isinstance(staged.size_bytes, int)
            or isinstance(staged.size_bytes, bool)
            or staged.size_bytes < 0
            or staged.size_bytes != source_size_bytes
        ):
            raise RuntimeError("media_transfer_provider_invalid_size")
