from __future__ import annotations

from dataclasses import dataclass

from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import paginate, validate_page
from src.model import BackgroundTaskRun, SystemNotification
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    NotificationBatchReadResponse,
    NotificationResource,
)
from src.service.system.activity.filters import (
    normalize_allowed_filter,
)

ALLOWED_NOTIFICATION_CATEGORIES = {"reminder", "info", "warning", "error"}

# 序列化/查询入口带 notification 前缀是刻意的：ActivityService 门面把本类与
# TaskRunService 多继承在一起，base 之间一旦出现同名方法，cls.xxx 就会被 MRO
# 静默解析到另一侧。名字不撞则 cls. 自引用天然安全，护栏见
# tests/test_architecture_boundaries.py。


@dataclass(frozen=True)
class NotificationDraft:
    category: str
    title: str
    content: str
    event_type: str | None = None
    dedupe_key: str | None = None
    resource_type: str | None = None
    resource_id: int | None = None
    related_task_run_id: int | None = None
    related_resource_type: str | None = None
    related_resource_id: int | None = None



def _detect_failed_summary(summary: dict | None) -> bool:
    if not summary:
        return False
    for key, value in summary.items():
        if isinstance(value, (int, float)) and value > 0 and "failed" in key:
            return True
    return False


class NotificationService:
    @staticmethod
    def to_notification_resource(notification: SystemNotification) -> NotificationResource:
        return NotificationResource.model_validate(
            {
                "id": notification.id,
                "category": notification.category,
                "title": notification.title,
                "content": notification.content,
                "event_type": notification.event_type,
                "dedupe_key": notification.dedupe_key,
                "resource_type": notification.resource_type,
                "resource_id": notification.resource_id,
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
                **cls._notification_values(draft, normalized_category)
            )
            return cls.to_notification_resource(notification)

    @staticmethod
    def _notification_values(
        draft: NotificationDraft, normalized_category: str | None
    ) -> dict:
        return {
            "category": normalized_category or "info",
            "title": draft.title,
            "content": draft.content,
            "event_type": draft.event_type,
            "dedupe_key": draft.dedupe_key,
            "resource_type": draft.resource_type,
            "resource_id": draft.resource_id,
            "related_task_run": draft.related_task_run_id,
            "related_resource_type": draft.related_resource_type,
            "related_resource_id": draft.related_resource_id,
        }

    @staticmethod
    def release_notification_dedupe_key(dedupe_key: str) -> int:
        """释放已解决事件的幂等键，保留通知历史以便下次同类事件重新提醒。"""
        return (
            SystemNotification.update(dedupe_key=None)
            .where(SystemNotification.dedupe_key == dedupe_key)
            .execute()
        )

    @classmethod
    def create_once(cls, draft: NotificationDraft) -> NotificationResource:
        """按 dedupe_key 原子创建通知，重复调用返回已有记录。"""
        dedupe_key = (draft.dedupe_key or "").strip()
        if not dedupe_key:
            raise ValueError("notification_dedupe_key_required")
        normalized_category = normalize_allowed_filter(
            draft.category,
            field_name="category",
            allowed_values=ALLOWED_NOTIFICATION_CATEGORIES,
        )
        values = cls._notification_values(draft, normalized_category)
        values["dedupe_key"] = dedupe_key
        with get_database().atomic():
            # PostgreSQL 唯一约束裁决并发竞争，避免先查后写的重复通知窗口。
            result = (
                SystemNotification.insert(**values)
                .on_conflict(
                    action="IGNORE",
                    conflict_target=(SystemNotification.dedupe_key,),
                )
                .returning(SystemNotification)
                .execute()
            )
            notification = next(iter(result), None)
            if notification is None:
                notification = SystemNotification.get(
                    SystemNotification.dedupe_key == dedupe_key
                )
            return cls.to_notification_resource(notification)

    @classmethod
    def notify_task_result(cls, task_run: BackgroundTaskRun, *, failed: bool) -> None:
        # 一条 task_run 最多只有一条终态通知。TaskRunService 的 active -> terminal
        # 行锁负责裁决赢家，这里的持久幂等键再防调用重放或事务重试重复插入通知。
        dedupe_key = f"task_run_result:{task_run.id}"
        if failed:
            cls.create_once(
                NotificationDraft(
                    category="error",
                    title=f"{task_run.task_name}执行失败",
                    content=task_run.error_message or "后台任务执行失败",
                    event_type="task_run_result",
                    dedupe_key=dedupe_key,
                    resource_type="task_run",
                    resource_id=task_run.id,
                    related_task_run_id=task_run.id,
                )
            )
            return
        # 常态成功和 skipped 不发通知，仅带 failed 统计的完成态提示 warning。
        if not _detect_failed_summary(task_run.result_summary or {}):
            return
        cls.create_once(
            NotificationDraft(
                category="warning",
                title=f"{task_run.task_name}已完成",
                content=task_run.result_text or "后台任务已完成",
                event_type="task_run_result",
                dedupe_key=dedupe_key,
                resource_type="task_run",
                resource_id=task_run.id,
                related_task_run_id=task_run.id,
            )
        )

    @classmethod
    def build_notification_query(cls, *, category: str | None = None, is_read: bool | None = None):
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
    def page_notifications(cls, query, *, page: int, page_size: int) -> PageResponse[NotificationResource]:
        return paginate(
            query,
            page,
            page_size,
            error_code="invalid_pagination",
            item_mapper=cls.to_notification_resource,
            response_model=PageResponse[NotificationResource],
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
        validate_page(page, page_size, error_code="invalid_pagination")
        return cls.page_notifications(
            cls.build_notification_query(category=category, is_read=is_read),
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
    def mark_notifications_read(cls, ids: list[int]) -> NotificationBatchReadResponse:
        now = utc_now_for_db()
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
            return NotificationBatchReadResponse(
                updated_count=updated_count, unread_count=unread_count
            )

    @classmethod
    def mark_all_notifications_read(cls) -> NotificationBatchReadResponse:
        now = utc_now_for_db()
        with get_database().atomic():
            updated_count = (
                SystemNotification.update(is_read=True, read_at=now, updated_at=now)
                .where(SystemNotification.is_read == False)
                .execute()
            )
            unread_count = cls.get_unread_count()
            return NotificationBatchReadResponse(
                updated_count=updated_count, unread_count=unread_count
            )
