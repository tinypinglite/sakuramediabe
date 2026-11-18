from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    ActivityBootstrapResource,
    NotificationResource,
    NotificationUpdateRequest,
    NotificationUpdateResponse,
    TaskRunAttentionResource,
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
    task_scope: str | None = Query(default=None),
):
    return ActivityService.get_activity_bootstrap(
        notification_category=notification_category,
        task_state=task_state,
        task_key=task_key,
        task_trigger_type=task_trigger_type,
        task_sort=task_sort,
        task_scope=task_scope,
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


@router.patch("/system/notifications", response_model=NotificationUpdateResponse)
def update_notifications_read_state(payload: NotificationUpdateRequest):
    return ActivityService.update_notifications_read_state(
        ids=None if payload.all else payload.ids,
        is_read=payload.is_read,
    )


@router.get("/system/task-runs/active", response_model=list[TaskRunResource])
def list_active_task_runs():
    return ActivityService.list_active_task_runs()


@router.get("/system/task-runs", response_model=PageResponse[TaskRunResource])
def list_task_runs(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    state: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    scope: str | None = Query(default=None),
):
    return ActivityService.list_task_runs(
        page=page,
        page_size=page_size,
        state=state,
        task_key=task_key,
        trigger_type=trigger_type,
        sort=sort,
        scope=scope,
    )


@router.get(
    "/system/task-runs/attention", response_model=list[TaskRunAttentionResource]
)
def list_attention_task_runs(limit: int = Query(default=50, ge=1, le=100)):
    # 静态路径必须声明在 /system/task-runs/{task_run_id} 之前，否则会被当成 id。
    return ActivityService.list_attention_task_runs(limit=limit)


@router.get("/system/task-runs/{task_run_id}", response_model=TaskRunResource)
def get_task_run(task_run_id: int):
    return ActivityService.get_task_run(task_run_id)
