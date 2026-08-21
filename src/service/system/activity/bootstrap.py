from contextlib import contextmanager

from src.model.base import get_database
from src.schema.system.activity import ActivityBootstrapResource
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_runs import TaskRunService

ACTIVITY_BOOTSTRAP_PAGE_SIZE = 20


@contextmanager
def activity_read_snapshot():
    # 首屏组合查询固定在同一快照，游标与资源列表不会跨时间点。
    with get_database().atomic(isolation_level="repeatable read"):
        yield


class ActivityBootstrapService:
    @staticmethod
    def get_activity_bootstrap(
        *,
        notification_category: str | None = None,
        task_state: str | None = None,
        task_key: str | None = None,
        task_trigger_type: str | None = None,
        task_sort: str | None = None,
    ) -> ActivityBootstrapResource:
        with activity_read_snapshot():
            notifications = NotificationService.page_notifications(
                NotificationService.build_notification_query(category=notification_category),
                page=1,
                page_size=ACTIVITY_BOOTSTRAP_PAGE_SIZE,
            )
            task_runs = TaskRunService.page_task_runs(
                TaskRunService.build_task_run_query(
                    state=task_state,
                    task_key=task_key,
                    trigger_type=task_trigger_type,
                    sort=task_sort,
                ),
                page=1,
                page_size=ACTIVITY_BOOTSTRAP_PAGE_SIZE,
            )
            return ActivityBootstrapResource(
                notifications=notifications,
                unread_count=NotificationService.get_unread_count(),
                active_task_runs=TaskRunService.list_active_task_runs(),
                task_runs=task_runs,
            )
