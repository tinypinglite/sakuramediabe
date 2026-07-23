from __future__ import annotations

from dataclasses import dataclass

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, SystemNotification
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    NotificationBatchReadResponse,
    NotificationReadResponse,
    NotificationResource,
)
from src.service.system.activity.events import SystemEventService
from src.service.system.activity.filters import (
    normalize_allowed_filter,
    validate_page,
)

ALLOWED_NOTIFICATION_CATEGORIES = {"reminder", "info", "warning", "error"}


@dataclass(frozen=True)
class NotificationDraft:
    category: str
    title: str
    content: str
    related_task_run_id: int | None = None
    related_resource_type: str | None = None
    related_resource_id: int | None = None


def _now():
    return utc_now_for_db()


def _detect_failed_summary(summary: dict | None) -> bool:
    if not summary:
        return False
    for key, value in summary.items():
        if isinstance(value, (int, float)) and value > 0 and "failed" in key:
            return True
    return False


class NotificationService:
    @staticmethod
    def to_resource(notification: SystemNotification) -> NotificationResource:
        return NotificationResource.model_validate(
            {
                "id": notification.id,
                "category": notification.category,
                "title": notification.title,
                "content": notification.content,
                "is_read": notification.is_read,
                "created_at": notification.created_at,
                "updated_at": notification.updated_at,
                "related_task_run_id": notification.related_task_run_id,
                "related_resource_type": notification.related_resource_type,
                "related_resource_id": notification.related_resource_id,
            }
        )

    @classmethod
    def create(cls, draft: NotificationDraft) -> NotificationResource:
        normalized_category = normalize_allowed_filter(
            draft.category,
            field_name="category",
            allowed_values=ALLOWED_NOTIFICATION_CATEGORIES,
        )
        with get_database().atomic():
            notification = SystemNotification.create(
                category=normalized_category or "info",
                title=draft.title,
                content=draft.content,
                related_task_run=draft.related_task_run_id,
                related_resource_type=draft.related_resource_type,
                related_resource_id=draft.related_resource_id,
            )
            SystemEventService.publish(
                event_type="notification_created",
                payload=cls.to_resource(notification).model_dump(mode="json"),
                resource_type="notification",
                resource_id=notification.id,
            )
            return cls.to_resource(notification)

    @classmethod
    def notify_task_result(cls, task_run: BackgroundTaskRun, *, failed: bool) -> None:
        if failed:
            cls.create(
                NotificationDraft(
                    category="error",
                    title=f"{task_run.task_name}执行失败",
                    content=task_run.error_message or "后台任务执行失败",
                    related_task_run_id=task_run.id,
                )
            )
            return
        # 常态成功和 skipped 不发通知，仅带 failed 统计的完成态提示 warning。
        if not _detect_failed_summary(task_run.result_summary or {}):
            return
        cls.create(
            NotificationDraft(
                category="warning",
                title=f"{task_run.task_name}已完成",
                content=task_run.result_text or "后台任务已完成",
                related_task_run_id=task_run.id,
            )
        )

    @classmethod
    def build_query(cls, *, category: str | None = None, is_read: bool | None = None):
        normalized_category = normalize_allowed_filter(
            category,
            field_name="category",
            allowed_values=ALLOWED_NOTIFICATION_CATEGORIES,
        )
        query = SystemNotification.select().order_by(
            SystemNotification.created_at.desc(), SystemNotification.id.desc()
        )
        if normalized_category is not None:
            query = query.where(SystemNotification.category == normalized_category)
        if is_read is not None:
            query = query.where(SystemNotification.is_read == is_read)
        return query

    @classmethod
    def page_query(cls, query, *, page: int, page_size: int) -> PageResponse[NotificationResource]:
        total = query.count()
        start = (page - 1) * page_size
        items = [cls.to_resource(item) for item in query.offset(start).limit(page_size)]
        return PageResponse[NotificationResource](
            items=items, page=page, page_size=page_size, total=total
        )

    @classmethod
    def list_notifications(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        category: str | None = None,
        is_read: bool | None = None,
    ) -> PageResponse[NotificationResource]:
        validate_page(page, page_size)
        return cls.page_query(
            cls.build_query(category=category, is_read=is_read),
            page=page,
            page_size=page_size,
        )

    @staticmethod
    def get_unread_count() -> int:
        return (
            SystemNotification.select()
            .where(SystemNotification.is_read == False)
            .count()
        )

    @classmethod
    def mark_notification_read(cls, notification_id: int) -> NotificationReadResponse:
        with get_database().atomic():
            notification = SystemNotification.get_or_none(
                SystemNotification.id == notification_id
            )
            if notification is None:
                raise ApiError(
                    404,
                    "notification_not_found",
                    "通知不存在",
                    {"notification_id": notification_id},
                )
            if not notification.is_read:
                notification.is_read = True
                notification.read_at = _now()
                notification.updated_at = _now()
                notification.save()
                SystemEventService.publish(
                    event_type="notification_updated",
                    payload=cls.to_resource(notification).model_dump(mode="json"),
                    resource_type="notification",
                    resource_id=notification.id,
                )
            return NotificationReadResponse(
                id=notification.id,
                is_read=notification.is_read,
                read_at=notification.read_at,
            )

    @classmethod
    def mark_notifications_read(cls, ids: list[int]) -> NotificationBatchReadResponse:
        now = _now()
        with get_database().atomic():
            updated_count = 0
            if ids:
                updated_count = (
                    SystemNotification.update(is_read=True, read_at=now, updated_at=now)
                    .where(
                        SystemNotification.id.in_(ids),
                        SystemNotification.is_read == False,
                    )
                    .execute()
                )
            unread_count = cls.get_unread_count()
            if updated_count:
                SystemEventService.publish(
                    event_type="notifications_read",
                    payload={
                        "ids": list(ids),
                        "updated_count": updated_count,
                        "unread_count": unread_count,
                    },
                )
            return NotificationBatchReadResponse(
                updated_count=updated_count, unread_count=unread_count
            )

    @classmethod
    def mark_all_notifications_read(cls) -> NotificationBatchReadResponse:
        now = _now()
        with get_database().atomic():
            updated_count = (
                SystemNotification.update(is_read=True, read_at=now, updated_at=now)
                .where(SystemNotification.is_read == False)
                .execute()
            )
            unread_count = cls.get_unread_count()
            if updated_count:
                SystemEventService.publish(
                    event_type="notifications_read_all",
                    payload={"updated_count": updated_count, "unread_count": unread_count},
                )
            return NotificationBatchReadResponse(
                updated_count=updated_count, unread_count=unread_count
            )
