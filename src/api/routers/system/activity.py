from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    ActivityBootstrapResource,
    NotificationBatchReadResponse,
    NotificationReadBatchRequest,
    NotificationResource,
    TaskRunResource,
)
from src.service.system import ActivityService

router = APIRouter(
    tags=["activity"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/system/activity/bootstrap", response_model=ActivityBootstrapResource)
def get_activity_bootstrap(
    notification_category: str | None = Query(default=None),
    task_state: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    task_trigger_type: str | None = Query(default=None),
    task_sort: str | None = Query(default=None),
):
    return ActivityService.get_activity_bootstrap(
        notification_category=notification_category,
        task_state=task_state,
        task_key=task_key,
        task_trigger_type=task_trigger_type,
        task_sort=task_sort,
    )


@router.get("/system/notifications", response_model=PageResponse[NotificationResource])
def list_notifications(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    category: str | None = Query(default=None),
    is_read: bool | None = Query(default=None),
):
    return ActivityService.list_notifications(
        page=page,
        page_size=page_size,
        category=category,
        is_read=is_read,
    )


@router.post(
    "/system/notifications/read",
    response_model=NotificationBatchReadResponse,
)
def mark_notifications_read(payload: NotificationReadBatchRequest):
    return ActivityService.mark_notifications_read(payload.ids)


@router.post(
    "/system/notifications/read-all",
    response_model=NotificationBatchReadResponse,
)
def mark_all_notifications_read():
    return ActivityService.mark_all_notifications_read()


@router.get("/system/task-runs", response_model=PageResponse[TaskRunResource])
def list_task_runs(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    state: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    sort: str | None = Query(default=None),
):
    return ActivityService.list_task_runs(
        page=page,
        page_size=page_size,
        state=state,
        task_key=task_key,
        trigger_type=trigger_type,
        sort=sort,
    )
