from __future__ import annotations

from loguru import logger
from peewee import fn

from src.common.runtime_time import utc_now_for_db
from src.model import MediaRapidUploadBatch, MediaRapidUploadItem
from src.service.transfers.media_rapid_upload.states import (
    BATCH_STATE_COMPLETED,
    BATCH_STATE_FAILED,
    BATCH_STATE_RUNNING,
    FAILURE_REASON_OTHER,
    FAILURE_REASON_RISK_CONTROL,
    GENERIC_CLEANUP_ERROR,
    GENERIC_ITEM_ERROR,
    GENERIC_RISK_CONTROL_ERROR,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_FAILED,
    ITEM_STATE_PENDING,
    ITEM_STATE_REMOTE_UPLOADED,
    ITEM_STATE_RETRIED,
    ITEM_STATE_RUNNING,
    ITEM_STATE_SUCCEEDED,
)


class MediaRapidUploadStateMachine:
    _ERROR_MESSAGE_MAX_LEN = 500

    @staticmethod
    def _mark_batch_running(batch: MediaRapidUploadBatch) -> None:
        batch.state = BATCH_STATE_RUNNING
        batch.started_at = utc_now_for_db()
        batch.updated_at = utc_now_for_db()
        batch.save()

    @staticmethod
    def _mark_item_running(item: MediaRapidUploadItem) -> None:
        item.state = ITEM_STATE_RUNNING
        item.started_at = utc_now_for_db()
        item.error_message = None
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _mark_item_remote_uploaded(item: MediaRapidUploadItem) -> None:
        item.state = ITEM_STATE_REMOTE_UPLOADED
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _mark_items_retried(item_ids: list[int]) -> None:
        if not item_ids:
            return
        (
            MediaRapidUploadItem.update(
                state=ITEM_STATE_RETRIED,
                updated_at=utc_now_for_db(),
            )
            .where(MediaRapidUploadItem.id.in_(item_ids))
            .execute()
        )

    @staticmethod
    def _mark_item_succeeded(item: MediaRapidUploadItem) -> None:
        item.state = ITEM_STATE_SUCCEEDED
        item.active_media_id = None
        item.error_message = None
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _mark_item_failed(
        item: MediaRapidUploadItem,
        message: str,
        *,
        failure_reason: str = FAILURE_REASON_OTHER,
    ) -> None:
        item.state = ITEM_STATE_FAILED
        item.active_media_id = None
        item.error_message = message
        item.failure_reason = failure_reason
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    @classmethod
    def _abort_remaining_for_risk_control(cls, batch, *, detail: str) -> int:
        """风控熔断：把本批所有尚未终结的 item 标为 risk_control 失败，返回标记数量。

        触发条件是某条 item 撞上 115 的 405（WAF 风控）。这些条目多半"本可秒传"，只是
        账号被临时冻结，故归为可重试失败并附风控说明，等冷却后重试即可；已推进到
        REMOTE_UPLOADED 的条目仍保留远端定位，重试会走 resume 而非重复秒传。
        """
        message = (detail or "").strip() or GENERIC_RISK_CONTROL_ERROR
        if len(message) > cls._ERROR_MESSAGE_MAX_LEN:
            message = message[: cls._ERROR_MESSAGE_MAX_LEN - 1] + "…"
        aborted = 0
        for item in MediaRapidUploadItem.select().where(
            MediaRapidUploadItem.batch == batch,
            MediaRapidUploadItem.state.in_(
                (ITEM_STATE_PENDING, ITEM_STATE_RUNNING, ITEM_STATE_REMOTE_UPLOADED)
            ),
        ):
            cls._mark_item_failed(
                item, message, failure_reason=FAILURE_REASON_RISK_CONTROL
            )
            aborted += 1
        return aborted

    @staticmethod
    def _mark_item_cleanup_failed(
        item: MediaRapidUploadItem,
        message: str = GENERIC_CLEANUP_ERROR,
    ) -> None:
        item.state = ITEM_STATE_CLEANUP_FAILED
        item.active_media_id = None
        item.error_message = message
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _sync_batch_counts(
        batch: MediaRapidUploadBatch,
        counts: dict[str, int],
    ) -> None:
        """运行中把累计计数持久化到批次行，让查询接口实时返回进度。

        此前计数只在 `_finish_batch` 落库，执行期间 `GET /media/rapid-uploads`
        返回的 succeeded/failed 恒为 0，前端秒传记录 tab 看不到实时进度。
        仅写计数与 updated_at，避免与 `_mark_item_*` 并发写同一行其它字段；
        最终值仍由 `_finish_batch` 从 items 重算兜底，本方法只负责"实时可见"。
        """
        batch.succeeded_count = counts["succeeded_count"]
        batch.failed_count = counts["failed_count"]
        batch.cleanup_failed_count = counts["cleanup_failed_count"]
        batch.updated_at = utc_now_for_db()
        batch.save(
            only=[
                MediaRapidUploadBatch.succeeded_count,
                MediaRapidUploadBatch.failed_count,
                MediaRapidUploadBatch.cleanup_failed_count,
                MediaRapidUploadBatch.updated_at,
            ]
        )

    @classmethod
    def _format_error(cls, exc: BaseException, *, fallback: str = GENERIC_ITEM_ERROR) -> str:
        text = str(exc).strip()
        if not text:
            return fallback
        if len(text) <= cls._ERROR_MESSAGE_MAX_LEN:
            return text
        return text[: cls._ERROR_MESSAGE_MAX_LEN - 1] + "…"

    @staticmethod
    def _item_counts(batch_id: int) -> dict[str, int]:
        rows = (
            MediaRapidUploadItem.select(
                MediaRapidUploadItem.state,
                fn.COUNT(MediaRapidUploadItem.id).alias("cnt"),
            )
            .where(MediaRapidUploadItem.batch == batch_id)
            .group_by(MediaRapidUploadItem.state)
        )
        by_state = {row.state: row.cnt for row in rows}
        return {
            "succeeded_count": by_state.get(ITEM_STATE_SUCCEEDED, 0),
            "failed_count": by_state.get(ITEM_STATE_FAILED, 0),
            "cleanup_failed_count": by_state.get(ITEM_STATE_CLEANUP_FAILED, 0),
        }

    @classmethod
    def _finish_batch(cls, batch: MediaRapidUploadBatch, *, force_failed: bool = False) -> None:
        counts = cls._item_counts(batch.id)
        batch.succeeded_count = counts["succeeded_count"]
        batch.failed_count = counts["failed_count"]
        batch.cleanup_failed_count = counts["cleanup_failed_count"]
        batch.state = BATCH_STATE_FAILED if force_failed else BATCH_STATE_COMPLETED
        batch.finished_at = utc_now_for_db()
        batch.updated_at = utc_now_for_db()
        batch.save()

    @classmethod
    def _fail_unfinished_items(cls, batch, *, detail: str) -> None:
        # 兜底路径拿不到具体条目的异常，只有批次级 detail；空 detail 时回退到通用文案。
        message = (detail or "").strip() or GENERIC_ITEM_ERROR
        if len(message) > cls._ERROR_MESSAGE_MAX_LEN:
            message = message[: cls._ERROR_MESSAGE_MAX_LEN - 1] + "…"
        for item in MediaRapidUploadItem.select().where(
            MediaRapidUploadItem.batch == batch,
            MediaRapidUploadItem.active_media_id.is_null(False),
        ):
            logger.warning(
                "Media rapid upload unfinished item recovered item_id={} detail={}",
                item.id,
                detail,
            )
            # 批次级兜底看不到具体条目原因，归为 OTHER（前端按"可重试"展示）。
            cls._mark_item_failed(item, message, failure_reason=FAILURE_REASON_OTHER)

    @classmethod
    def _fail_unstarted_batch(cls, batch, detail: str) -> None:
        cls._fail_unfinished_items(batch, detail=detail)
        cls._finish_batch(batch, force_failed=True)
