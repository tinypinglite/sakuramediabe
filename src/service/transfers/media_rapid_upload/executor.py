from __future__ import annotations

import asyncio

from loguru import logger

from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.lib.cloud115 import Cloud115RiskControlError
from src.model import (
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    get_database,
)
from src.service.cloud115 import cloud115_client_for, require_cloud115_library
from src.service.system import ActivityService
from src.service.transfers.cloud115_import_common import Cloud115TargetDirResolver
from src.service.transfers.media_rapid_upload.notifications import (
    create_media_rapid_upload_notification,
)
from src.service.transfers.media_rapid_upload.states import (
    BATCH_STATE_FAILED,
    FAILURE_REASON_REMOTE_ERROR,
    GENERIC_CLEANUP_ERROR,
    GENERIC_RISK_CONTROL_ERROR,
    ITEM_ACTION_CLEANUP_ONLY,
    ITEM_ACTION_RESUME_REMOTE,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_FAILED,
    ITEM_STATE_SUCCEEDED,
    TASK_KEY,
)
from src.service.transfers.media_rapid_upload.types import RapidUploadFailure

class MediaRapidUploadExecutor:
    @classmethod
    def _run_batch(cls, batch_id: int, task_run_id: int) -> dict:
        ensure_database_ready()

        def _task(reporter):
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            cls._mark_batch_running(batch)
            asyncio.run(cls._process_batch(batch, reporter))
            cls._finish_batch(batch)
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            return {
                "rapid_upload_batch_id": batch.id,
                "total_count": batch.total_count,
                "succeeded_count": batch.succeeded_count,
                "failed_count": batch.failed_count,
                "cleanup_failed_count": batch.cleanup_failed_count,
            }

        try:
            result = ActivityService.run_task(
                task_key=TASK_KEY,
                trigger_type="internal",
                task_run_id=task_run_id,
                func=_task,
                notify_result=False,
            )
        except Exception as exc:
            logger.exception("Media rapid upload batch crashed batch_id={}", batch_id)
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            cls._fail_unfinished_items(batch, detail=str(exc))
            cls._finish_batch(batch, force_failed=True)
            result = {
                "rapid_upload_batch_id": batch_id,
                "total_count": batch.total_count,
                "succeeded_count": batch.succeeded_count,
                "failed_count": batch.failed_count,
                "cleanup_failed_count": batch.cleanup_failed_count,
            }
        cls._ensure_completion_notification(batch_id, task_run_id)
        return result

    @classmethod
    async def _process_batch(cls, batch, reporter) -> None:
        target_library = MediaLibrary.get_by_id(batch.target_library_id)
        config = require_cloud115_library(target_library)
        items = list(
            MediaRapidUploadItem.select()
            .where(MediaRapidUploadItem.batch == batch)
            .order_by(MediaRapidUploadItem.id.asc())
        )
        counts = {"succeeded_count": 0, "failed_count": 0, "cleanup_failed_count": 0}
        # 全局请求限速：把批内所有 115 请求匀速化，规避 webapi 前置 WAF 的风控阈值。
        min_interval = settings.downloads.cloud115_rapid_upload_min_interval_seconds
        async with cloud115_client_for(
            target_library, min_request_interval=min_interval
        ) as client:
            # 批次级目录缓存：整批只翻一次 jav//videos/，杜绝每条 item 列目录的风控请求。
            resolver = Cloud115TargetDirResolver(client, root_cid=config["root_cid"])
            for index, item in enumerate(items, start=1):
                cls._mark_item_running(item)
                try:
                    if item.action == ITEM_ACTION_CLEANUP_ONLY:
                        try:
                            cls._cleanup_source(item)
                        except OSError as exc:
                            cls._mark_item_cleanup_failed(
                                item,
                                cls._format_error(exc, fallback=GENERIC_CLEANUP_ERROR),
                            )
                    elif item.action == ITEM_ACTION_RESUME_REMOTE:
                        media = cls._require_current_local_media(item)
                        await cls._resume_remote_item(
                            client,
                            item=item,
                            media=media,
                            target_library=target_library,
                        )
                    else:
                        media = cls._require_current_local_media(item)
                        target_cid, target_name = await cls._prepare_target_dir_and_name(
                            resolver,
                            media=media,
                            source_path=item.source_path,
                        )
                        await cls._rapid_upload_item(
                            client,
                            item=item,
                            media=media,
                            target_library=target_library,
                            target_cid=target_cid,
                            target_name=target_name,
                        )
                except Cloud115RiskControlError as exc:
                    # 风控熔断：账号已被 WAF 冻结，继续发请求只会制造更多 405、加深封禁。
                    # 立即把本条及后续未处理条目标为可重试的 risk_control 失败并停批。
                    logger.warning(
                        "Media rapid upload hit risk control, aborting batch_id={} at item_id={} detail={}",
                        batch.id,
                        item.id,
                        exc,
                    )
                    aborted = cls._abort_remaining_for_risk_control(
                        batch, detail=cls._format_error(exc, fallback=GENERIC_RISK_CONTROL_ERROR)
                    )
                    counts["failed_count"] += aborted
                    reporter.emit(
                        current=len(items),
                        total=len(items),
                        text=f"触发 115 风控，已中止本批（{aborted} 个待冷却后重试）",
                        summary_patch=counts,
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        "Media rapid upload item failed batch_id={} item_id={} media_id={} detail={}",
                        batch.id,
                        item.id,
                        item.media_id,
                        exc,
                    )
                    failure_reason = (
                        exc.failure_reason
                        if isinstance(exc, RapidUploadFailure)
                        else FAILURE_REASON_REMOTE_ERROR
                    )
                    cls._mark_item_failed(
                        item,
                        cls._format_error(exc),
                        failure_reason=failure_reason,
                    )
                if item.state == ITEM_STATE_SUCCEEDED:
                    counts["succeeded_count"] += 1
                elif item.state == ITEM_STATE_FAILED:
                    counts["failed_count"] += 1
                elif item.state == ITEM_STATE_CLEANUP_FAILED:
                    counts["cleanup_failed_count"] += 1
                reporter.emit(
                    current=index,
                    total=len(items),
                    text=f"已处理 {index}/{len(items)} 个媒体",
                    summary_patch=counts,
                )

    @classmethod
    def _ensure_completion_notification(cls, batch_id: int, task_run_id: int) -> None:
        with get_database().atomic():
            batch = (
                MediaRapidUploadBatch.select()
                .where(MediaRapidUploadBatch.id == batch_id)
                .for_update()
                .get()
            )
            if batch.completion_notification_id is not None:
                return
            notification = create_media_rapid_upload_notification(
                batch_id=batch.id,
                task_run_id=task_run_id,
                total_count=batch.total_count,
                succeeded_count=batch.succeeded_count,
                failed_count=batch.failed_count,
                cleanup_failed_count=batch.cleanup_failed_count,
                batch_failed=batch.state == BATCH_STATE_FAILED,
            )
            batch.completion_notification = notification.id
            batch.updated_at = utc_now_for_db()
            batch.save(
                only=[
                    MediaRapidUploadBatch.completion_notification,
                    MediaRapidUploadBatch.updated_at,
                ]
            )
