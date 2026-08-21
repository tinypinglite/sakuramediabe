from __future__ import annotations

from pathlib import Path

from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    Media,
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    get_database,
)
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.rapid_upload import MediaRapidUploadTriggerResponse
from src.service.cloud115 import require_cloud115_library
from src.service.system import ActivityService
from src.service.transfers.rapid_upload.states import (
    BATCH_STATE_COMPLETED,
    BATCH_STATE_FAILED,
    BATCH_STATE_PENDING,
    ITEM_ACTION_CLEANUP_ONLY,
    ITEM_ACTION_RAPID_UPLOAD,
    ITEM_ACTION_RESUME_REMOTE,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_PENDING,
    RETRYABLE_ITEM_STATES,
    TASK_KEY,
)
from src.service.transfers.rapid_upload.types import ItemSpec
from src.service.transfers.shared.write_mutex import cloud115_write_mutex_key


class MediaRapidUploadCommandService:
    @classmethod
    def trigger_batch(
        cls,
        *,
        media_ids: list[int],
        target_library_id: int,
    ) -> MediaRapidUploadTriggerResponse:
        if not media_ids or len(media_ids) > 1000 or len(media_ids) != len(set(media_ids)):
            raise ApiError(
                422,
                "invalid_media_rapid_upload_selection",
                "media_ids 必须包含 1 至 1000 个不重复的媒体 ID",
            )
        target_library = cls._require_target_library(target_library_id)
        specs = [cls._build_local_item_spec(media_id) for media_id in media_ids]
        return cls._create_and_submit_batch(
            target_library=target_library,
            specs=specs,
            retry_of_batch_id=None,
        )

    @classmethod
    def retry_batch(cls, batch_id: int) -> MediaRapidUploadTriggerResponse:
        original = cls._require_batch(batch_id)
        if original.state not in {BATCH_STATE_COMPLETED, BATCH_STATE_FAILED}:
            raise ApiError(
                409,
                "media_rapid_upload_batch_in_progress",
                "秒传批次仍在执行中",
                {"rapid_upload_batch_id": batch_id},
            )
        target_library = cls._require_target_library(original.target_library_id)
        specs: list[ItemSpec] = []
        items = (
            MediaRapidUploadItem.select()
            .where(
                MediaRapidUploadItem.batch == original,
                MediaRapidUploadItem.state.in_(RETRYABLE_ITEM_STATES),
            )
            .order_by(MediaRapidUploadItem.id.asc())
        )
        for item in items:
            if item.media_id is None:
                continue
            if item.state == ITEM_STATE_CLEANUP_FAILED:
                specs.append(cls._build_cleanup_retry_spec(item))
            elif cls._has_remote_locator(item):
                specs.append(cls._build_remote_resume_spec(item))
            else:
                specs.append(
                    cls._build_local_item_spec(
                        item.media_id,
                        retry_source_item_id=item.id,
                    )
                )
        if not specs:
            raise ApiError(
                422,
                "media_rapid_upload_no_retryable_items",
                "该批次没有可重试的失败媒体",
                {"rapid_upload_batch_id": batch_id},
            )
        return cls._create_and_submit_batch(
            target_library=target_library,
            specs=specs,
            retry_of_batch_id=original.id,
        )

    @classmethod
    def _create_and_submit_batch(
        cls,
        *,
        target_library: MediaLibrary,
        specs: list[ItemSpec],
        retry_of_batch_id: int | None,
    ) -> MediaRapidUploadTriggerResponse:
        mutex_key = cloud115_write_mutex_key(target_library)
        batch: MediaRapidUploadBatch | None = None
        try:
            with get_database().atomic():
                # 批次和队列行同事务提交，worker 不会看到未完成的批次。
                task_run = ActivityService.create_task_run(
                    task_key=TASK_KEY,
                    task_name=f"批量媒体秒传（{len(specs)} 个）",
                    trigger_type="manual",
                    mutex_key=mutex_key,
                    params={},
                )
                batch = MediaRapidUploadBatch.create(
                    target_library=target_library,
                    retry_of_batch=retry_of_batch_id,
                    task_run=task_run,
                    state=BATCH_STATE_PENDING,
                    total_count=len(specs),
                )
                for spec in specs:
                    MediaRapidUploadItem.create(
                        batch=batch,
                        media=spec.media_id,
                        active_media_id=spec.media_id,
                        action=spec.action,
                        state=ITEM_STATE_PENDING,
                        source_library_id=spec.source_library_id,
                        source_path=spec.source_path,
                        source_size_bytes=spec.source_size_bytes,
                        source_mtime_ns=spec.source_mtime_ns,
                        source_sha1=spec.source_sha1,
                        target_cid=spec.target_cid,
                        target_fid=spec.target_fid,
                        target_pickcode=spec.target_pickcode,
                        target_name=spec.target_name,
                    )
                retry_source_item_ids = [
                    spec.retry_source_item_id
                    for spec in specs
                    if spec.retry_source_item_id is not None
                ]
                if retry_source_item_ids:
                    # 新批次接管失败项后，旧项不再重复参与后续重试或删除保护。
                    cls._mark_items_retried(retry_source_item_ids)
                BackgroundTaskRun.update(
                    params={"rapid_upload_batch_id": batch.id}
                ).where(BackgroundTaskRun.id == task_run.id).execute()
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(mutex_key)
            if blocking is not None:
                raise ApiError(
                    409,
                    "cloud115_write_task_conflict",
                    "已有 115 导入或秒传任务",
                    {"blocking_task_run_id": blocking.id},
                ) from exc
            raise ApiError(
                409,
                "media_rapid_upload_media_conflict",
                "选择的媒体已在其它秒传批次中",
            ) from exc
        except Exception as exc:
            raise ApiError(
                502,
                "media_rapid_upload_launch_failed",
                "秒传任务入队失败",
                {"detail": str(exc)},
            ) from exc
        return MediaRapidUploadTriggerResponse(
            rapid_upload_batch_id=batch.id,
            task_run_id=task_run.id,
            status="accepted",
        )

    @classmethod
    def _build_local_item_spec(
        cls,
        media_id: int,
        *,
        retry_source_item_id: int | None = None,
    ) -> ItemSpec:
        media = Media.get_or_none(Media.id == media_id)
        if media is None:
            raise ApiError(404, "media_not_found", "媒体不存在", {"media_id": media_id})
        if media.library_id is None or media.library.backend != MediaLibraryBackend.LOCAL.value:
            raise ApiError(
                422,
                "media_rapid_upload_source_not_local",
                "只能秒传本地媒体库中的媒体",
                {"media_id": media_id},
            )
        if not media.path:
            raise ApiError(422, "media_file_not_found", "媒体没有本地文件", {"media_id": media_id})
        source = Path(media.path)
        try:
            stat = source.stat()
        except OSError as exc:
            raise ApiError(
                422,
                "media_file_not_found",
                "媒体本地文件不存在",
                {"media_id": media_id},
            ) from exc
        if not source.is_file():
            raise ApiError(422, "media_file_not_found", "媒体本地文件不存在", {"media_id": media_id})
        return ItemSpec(
            media_id=media.id,
            action=ITEM_ACTION_RAPID_UPLOAD,
            source_library_id=media.library_id,
            source_path=str(source),
            source_size_bytes=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            retry_source_item_id=retry_source_item_id,
        )

    @staticmethod
    def _build_cleanup_retry_spec(item: MediaRapidUploadItem) -> ItemSpec:
        media = Media.get_or_none(Media.id == item.media_id)
        if media is None:
            raise ApiError(
                422,
                "media_rapid_upload_cleanup_target_missing",
                "待清理媒体已不存在，无法重试",
                {"media_id": item.media_id},
            )
        return ItemSpec(
            media_id=item.media_id,
            action=ITEM_ACTION_CLEANUP_ONLY,
            source_library_id=item.source_library_id,
            source_path=item.source_path,
            source_size_bytes=item.source_size_bytes,
            source_mtime_ns=item.source_mtime_ns,
            source_sha1=item.source_sha1,
            target_cid=item.target_cid,
            target_fid=item.target_fid,
            target_pickcode=item.target_pickcode,
            target_name=item.target_name,
            retry_source_item_id=item.id,
        )

    @staticmethod
    def _build_remote_resume_spec(item: MediaRapidUploadItem) -> ItemSpec:
        return ItemSpec(
            media_id=item.media_id,
            action=ITEM_ACTION_RESUME_REMOTE,
            source_library_id=item.source_library_id,
            source_path=item.source_path,
            source_size_bytes=item.source_size_bytes,
            source_mtime_ns=item.source_mtime_ns,
            source_sha1=item.source_sha1,
            target_cid=item.target_cid,
            target_fid=item.target_fid,
            target_pickcode=item.target_pickcode,
            target_name=item.target_name,
            retry_source_item_id=item.id,
        )

    @staticmethod
    def _has_remote_locator(item: MediaRapidUploadItem) -> bool:
        return all(
            (
                item.source_sha1,
                item.target_cid,
                item.target_fid,
                item.target_pickcode,
            )
        )

    @staticmethod
    def _require_target_library(library_id: int) -> MediaLibrary:
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(
                404,
                "media_library_not_found",
                "媒体库不存在",
                {"library_id": library_id},
            )
        if library.backend != MediaLibraryBackend.CLOUD115.value:
            raise ApiError(
                422,
                "media_rapid_upload_target_not_cloud115",
                "秒传目标必须是 115 媒体库",
                {"library_id": library_id},
            )
        require_cloud115_library(library)
        return library
