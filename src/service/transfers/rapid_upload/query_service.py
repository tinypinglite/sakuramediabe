from peewee import fn

from src.api.exception.errors import ApiError
from src.model import MediaRapidUploadBatch, MediaRapidUploadItem
from src.schema.common.pagination import PageResponse
from src.schema.transfers.rapid_upload import (
    MediaRapidUploadBatchListItemResource,
    MediaRapidUploadBatchResource,
)
from src.service.transfers.rapid_upload.states import (
    BATCH_STATE_PENDING,
    BATCH_STATE_RUNNING,
    FAILURE_REASON_NOT_HIT,
    IN_PROGRESS_ITEM_STATES,
    ITEM_STATE_CLEANUP_FAILED,
    ITEM_STATE_FAILED,
    ITEM_STATE_RETRIED,
    ITEM_STATE_SUCCEEDED,
    PUBLIC_STATUS_CLEANUP_FAILED,
    PUBLIC_STATUS_FAILED,
    PUBLIC_STATUS_IN_PROGRESS,
    PUBLIC_STATUS_NOT_HIT,
)


class MediaRapidUploadQueryService:
    @classmethod
    def list_batches(cls, *, page: int = 1, page_size: int = 20) -> PageResponse:
        if page < 1 or page_size < 1 or page_size > 100:
            raise ApiError(422, "invalid_pagination", "分页参数非法")
        query = MediaRapidUploadBatch.select().order_by(
            MediaRapidUploadBatch.id.desc()
        )
        total = query.count()
        rows = list(query.offset((page - 1) * page_size).limit(page_size))
        return PageResponse[MediaRapidUploadBatchListItemResource](
            items=[MediaRapidUploadBatchListItemResource.from_model(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_batch(cls, batch_id: int) -> MediaRapidUploadBatchResource:
        batch = cls._require_batch(batch_id)
        items = list(
            MediaRapidUploadItem.select()
            .where(MediaRapidUploadItem.batch == batch)
            .order_by(MediaRapidUploadItem.id.asc())
        )
        return MediaRapidUploadBatchResource.from_model(batch, items=items)

    @classmethod
    def get_latest_status_by_media(cls, media_ids: list[int]) -> dict[int, str]:
        """按 media_id 取最新一次非 retried 的 item，映射成对外紧凑 status。

        返回值域：``'not_hit' | 'failed' | 'cleanup_failed' | 'in_progress'``；
        缺席 key 即"该 media 未参与过秒传"或"最新一次已 succeeded"（succeeded 意味着 media
        已切云端，本地视角不需要该提示）。
        """
        if not media_ids:
            return {}
        # PostgreSQL DISTINCT ON：按 media_id 分组、id DESC 取每组第一条，一趟走
        # (media_id) 索引即可拿到"最新一次非 retried item"；比 IN(SELECT MAX GROUP BY)
        # 子查询少一次扫描。state != 'retried' 排除重试接管后的老条目。
        rows = (
            MediaRapidUploadItem.select(
                MediaRapidUploadItem.media,
                MediaRapidUploadItem.state,
                MediaRapidUploadItem.failure_reason,
            )
            .where(
                MediaRapidUploadItem.media.in_(media_ids),
                MediaRapidUploadItem.state != ITEM_STATE_RETRIED,
            )
            .order_by(MediaRapidUploadItem.media, MediaRapidUploadItem.id.desc())
            .distinct(MediaRapidUploadItem.media)
        )
        result: dict[int, str] = {}
        for row in rows:
            status = cls._map_item_to_public_status(row.state, row.failure_reason)
            if status is not None:
                result[row.media_id] = status
        return result

    @staticmethod
    def _map_item_to_public_status(state: str, failure_reason: str | None) -> str | None:
        # 内部 state/reason 到对外紧凑值域的唯一映射点，新增值域时改这一处。
        if state == ITEM_STATE_SUCCEEDED:
            return None
        if state in IN_PROGRESS_ITEM_STATES:
            return PUBLIC_STATUS_IN_PROGRESS
        if state == ITEM_STATE_CLEANUP_FAILED:
            return PUBLIC_STATUS_CLEANUP_FAILED
        if state == ITEM_STATE_FAILED:
            if failure_reason == FAILURE_REASON_NOT_HIT:
                return PUBLIC_STATUS_NOT_HIT
            return PUBLIC_STATUS_FAILED
        return None

    @classmethod
    def media_id_subquery_for_status(cls, public_status: str):
        """返回一个 SELECT media_id 的 peewee 子查询，覆盖"最新一次非 retried item
        命中给定对外 status"的所有 media。调用方直接 ``Media.id.in_(subq)``。

        用相关子查询 (MAX(id)) 定位"当前 item 是最新"，再叠加 (state, failure_reason)
        predicate。这样避免"先按 filter 再 DISTINCT ON"造成的语义错位：
        举例 media A 依次有 failed(not_hit) / failed(file_changed) / succeeded 三条时，
        筛 not_hit 不应命中，因为最新已成功。

        本方法只处理"有状态"的 4 个值；none 走 :meth:`active_media_id_subquery`
        + ``Media.id.not_in(...)`` 反选。
        """
        predicate = cls._predicate_for_public_status(public_status)
        max_id = cls._latest_non_retried_max_id_subquery()
        return (
            MediaRapidUploadItem.select(MediaRapidUploadItem.media).where(
                MediaRapidUploadItem.id == max_id,
                predicate,
            )
        )

    @classmethod
    def active_media_id_subquery(cls):
        """当前"最新非 retried item 存在且非 succeeded"的 media_id 集合。

        专供 list_media 的 ``rapid_upload_status='none'`` 反选：
        ``Media.id.not_in(active_media_id_subquery())`` 等价于"从未参与秒传"
        或"最近一次已 succeeded 切云端"。
        """
        max_id = cls._latest_non_retried_max_id_subquery()
        return (
            MediaRapidUploadItem.select(MediaRapidUploadItem.media).where(
                MediaRapidUploadItem.id == max_id,
                MediaRapidUploadItem.state != ITEM_STATE_SUCCEEDED,
            )
        )

    @staticmethod
    def _latest_non_retried_max_id_subquery():
        # 相关子查询：outer 每条 item 都取自己 media_id 下最新非 retried item.id；
        # 别名让 inner.media 能不歧义地绑到 outer 的 MediaRapidUploadItem.media。
        inner = MediaRapidUploadItem.alias()
        return (
            inner.select(fn.MAX(inner.id))
            .where(
                inner.media == MediaRapidUploadItem.media,
                inner.state != ITEM_STATE_RETRIED,
            )
        )

    @staticmethod
    def _predicate_for_public_status(public_status: str):
        # (state, failure_reason) → predicate 表达式；对外 4 个 status 的解释来源。
        # 语义反映了 _map_item_to_public_status 的分类逻辑，改一处两边一起调。
        if public_status == PUBLIC_STATUS_NOT_HIT:
            return (
                (MediaRapidUploadItem.state == ITEM_STATE_FAILED)
                & (MediaRapidUploadItem.failure_reason == FAILURE_REASON_NOT_HIT)
            )
        if public_status == PUBLIC_STATUS_FAILED:
            # failed 桶收纳所有非 not_hit 的失败原因（含 NULL：老数据未回填的 item）。
            return (
                (MediaRapidUploadItem.state == ITEM_STATE_FAILED)
                & (
                    (MediaRapidUploadItem.failure_reason != FAILURE_REASON_NOT_HIT)
                    | MediaRapidUploadItem.failure_reason.is_null()
                )
            )
        if public_status == PUBLIC_STATUS_CLEANUP_FAILED:
            return MediaRapidUploadItem.state == ITEM_STATE_CLEANUP_FAILED
        if public_status == PUBLIC_STATUS_IN_PROGRESS:
            return MediaRapidUploadItem.state.in_(list(IN_PROGRESS_ITEM_STATES))
        raise ApiError(
            422,
            "invalid_media_filter",
            "Invalid rapid_upload_status",
            {"rapid_upload_status": public_status},
        )

    @staticmethod
    def has_active_media(media_id: int) -> bool:
        return (
            MediaRapidUploadItem.select()
            .where(
                (MediaRapidUploadItem.active_media_id == media_id)
                | (
                    (MediaRapidUploadItem.media == media_id)
                    & (MediaRapidUploadItem.state == ITEM_STATE_CLEANUP_FAILED)
                )
            )
            .exists()
        )

    @staticmethod
    def has_active_library(library_id: int) -> bool:
        return (
            MediaRapidUploadBatch.select()
            .where(
                MediaRapidUploadBatch.target_library == library_id,
                MediaRapidUploadBatch.state.in_((BATCH_STATE_PENDING, BATCH_STATE_RUNNING)),
            )
            .exists()
        )

    @staticmethod
    def has_library_history(library_id: int) -> bool:
        """秒传历史保留目标媒体库外键，删除前必须显式拦截。"""
        return (
            MediaRapidUploadBatch.select()
            .where(MediaRapidUploadBatch.target_library == library_id)
            .exists()
        )

    @staticmethod
    def _require_batch(batch_id: int) -> MediaRapidUploadBatch:
        batch = MediaRapidUploadBatch.get_or_none(MediaRapidUploadBatch.id == batch_id)
        if batch is None:
            raise ApiError(
                404,
                "media_rapid_upload_batch_not_found",
                "秒传批次不存在",
                {"rapid_upload_batch_id": batch_id},
            )
        return batch
