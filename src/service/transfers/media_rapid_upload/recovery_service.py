from __future__ import annotations

from loguru import logger

from src.model import Media, MediaRapidUploadBatch, MediaRapidUploadItem
from src.service.transfers.media_rapid_upload.states import (
    BATCH_STATE_COMPLETED,
    BATCH_STATE_FAILED,
    BATCH_STATE_PENDING,
    BATCH_STATE_RUNNING,
    FAILURE_REASON_OTHER,
    GENERIC_CLEANUP_ERROR,
    GENERIC_ITEM_ERROR,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_FAILED,
    ITEM_STATE_SUCCEEDED,
)


class MediaRapidUploadRecoveryService:
    @classmethod
    def recover_interrupted_batches(cls) -> dict[str, int]:
        """启动时把中断批次收敛为可重试终态；已切云端的条目仅重试清源。"""
        recovered = 0
        batches = MediaRapidUploadBatch.select().where(
            MediaRapidUploadBatch.state.in_((BATCH_STATE_PENDING, BATCH_STATE_RUNNING))
        )
        for batch in batches:
            for item in MediaRapidUploadItem.select().where(
                MediaRapidUploadItem.batch == batch,
                MediaRapidUploadItem.state.not_in(
                    (ITEM_STATE_SUCCEEDED, ITEM_STATE_FAILED, ITEM_STATE_CLEANUP_FAILED)
                ),
            ):
                media = Media.get_or_none(Media.id == item.media_id)
                if (
                    media is not None
                    and media.library_id == batch.target_library_id
                    and media.path is None
                    and item.target_pickcode
                ):
                    cls._mark_item_cleanup_failed(item, GENERIC_CLEANUP_ERROR)
                else:
                    # 启动兜底不知道原始失败原因，标 OTHER 让前端按可重试处理。
                    cls._mark_item_failed(
                        item,
                        GENERIC_ITEM_ERROR,
                        failure_reason=FAILURE_REASON_OTHER,
                    )
            cls._finish_batch(batch, force_failed=True)
            recovered += 1

        # 已结束但因短暂故障漏发通知的批次，也在启动恢复时补齐。
        for batch in MediaRapidUploadBatch.select().where(
            MediaRapidUploadBatch.state.in_((BATCH_STATE_COMPLETED, BATCH_STATE_FAILED)),
            MediaRapidUploadBatch.completion_notification.is_null(True),
        ):
            if batch.task_run_id is None:
                continue
            try:
                cls._ensure_completion_notification(batch.id, batch.task_run_id)
            except Exception:
                # 通知投递不应阻断其他中断批次的状态收敛；下次启动会继续补发。
                logger.exception(
                    "Media rapid upload recovery notification failed batch_id={}",
                    batch.id,
                )
        return {"recovered_count": recovered}
