from __future__ import annotations

from typing import Any

from src.api.exception.errors import ApiError
from src.common.service_helpers import validate_page
from src.model import BackgroundTaskRun, BackgroundTaskRunItem
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import TaskRunItemResource


class TaskRunItemService:
    """通用批次任务明细服务，按 task_run_id 记录每个处理对象的结果。"""

    ALLOWED_ITEM_STATES = {"pending", "running", "succeeded", "skipped", "failed", "canceled"}

    @staticmethod
    def _normalize_state(state: str) -> str:
        normalized = (state or "").strip().lower()
        if normalized not in TaskRunItemService.ALLOWED_ITEM_STATES:
            raise ValueError(f"invalid_task_run_item_state: {state}")
        return normalized

    @staticmethod
    def _merge_extra(existing_extra: object, extra_patch: dict[str, Any] | None) -> dict | list | None:
        if not extra_patch:
            if isinstance(existing_extra, (dict, list)):
                return existing_extra
            return None
        merged: dict[str, Any] = {}
        if isinstance(existing_extra, dict):
            merged.update(existing_extra)
        merged.update(extra_patch)
        return merged

    @classmethod
    def upsert_item(
        cls,
        *,
        task_run_id: int,
        item_type: str,
        item_key: str,
        state: str = "pending",
        source_path: str | None = None,
        target_path: str | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        extra_patch: dict[str, Any] | None = None,
    ) -> BackgroundTaskRunItem:
        normalized_state = cls._normalize_state(state)
        item, _ = BackgroundTaskRunItem.get_or_create(
            task_run=task_run_id,
            item_type=item_type,
            item_key=item_key,
            defaults={
                "state": normalized_state,
                "source_path": source_path,
                "target_path": target_path,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "related_resource_type": related_resource_type,
                "related_resource_id": related_resource_id,
                "error_code": error_code,
                "error_detail": error_detail,
                "extra": extra_patch,
            },
        )
        item.state = normalized_state
        if source_path is not None:
            item.source_path = source_path
        if target_path is not None:
            item.target_path = target_path
        if resource_type is not None:
            item.resource_type = resource_type
        if resource_id is not None:
            item.resource_id = resource_id
        if related_resource_type is not None:
            item.related_resource_type = related_resource_type
        if related_resource_id is not None:
            item.related_resource_id = related_resource_id
        item.error_code = error_code
        item.error_detail = error_detail
        item.extra = cls._merge_extra(item.extra, extra_patch)
        item.save()
        return item

    @classmethod
    def mark_canceled_pending_items(cls, task_run_id: int, *, item_type: str | None = None) -> int:
        query = BackgroundTaskRunItem.update(state="canceled").where(
            BackgroundTaskRunItem.task_run == task_run_id,
            BackgroundTaskRunItem.state.in_(("pending", "running")),
        )
        if item_type is not None:
            query = query.where(BackgroundTaskRunItem.item_type == item_type)
        return query.execute()

    @classmethod
    def list_items(
        cls,
        *,
        task_run_id: int,
        page: int = 1,
        page_size: int = 20,
        state: str | None = None,
        item_type: str | None = None,
    ) -> PageResponse[TaskRunItemResource]:
        validate_page(page, page_size, error_code="invalid_task_run_item_filter")
        if not BackgroundTaskRun.select().where(BackgroundTaskRun.id == task_run_id).exists():
            raise ApiError(404, "task_run_not_found", "任务不存在", {"task_run_id": task_run_id})
        query = BackgroundTaskRunItem.select().where(BackgroundTaskRunItem.task_run == task_run_id)
        if state is not None:
            query = query.where(BackgroundTaskRunItem.state == cls._normalize_state(state))
        if item_type is not None:
            query = query.where(BackgroundTaskRunItem.item_type == item_type.strip())
        query = query.order_by(BackgroundTaskRunItem.id.asc())
        total = query.count()
        items = [
            TaskRunItemResource.from_attributes_model(item)
            for item in query.offset((page - 1) * page_size).limit(page_size)
        ]
        return PageResponse[TaskRunItemResource](items=items, page=page, page_size=page_size, total=total)
