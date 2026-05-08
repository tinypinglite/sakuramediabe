from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    ActivityBootstrapResource,
    NotificationReadResponse,
    NotificationResource,
    TaskRunResource,
)
from src.schema.system.resource_task_state import (
    ResourceTaskDefinitionResource,
    ResourceTaskRecordResource,
)
from src.service.system import ActivityService, SystemEventService
from src.service.system.resource_task_state_service import ResourceTaskStateService

router = APIRouter(
    tags=["activity"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/system/activity/bootstrap", response_model=ActivityBootstrapResource)
def get_activity_bootstrap(
    notification_category: str | None = Query(default=None),
    notification_archived: bool = Query(default=False),
    task_state: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    task_trigger_type: str | None = Query(default=None),
    task_sort: str | None = Query(default=None),
):
    return ActivityService.get_activity_bootstrap(
        notification_category=notification_category,
        notification_archived=notification_archived,
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
    archived: bool = Query(default=False),
):
    return ActivityService.list_notifications(
        page=page,
        page_size=page_size,
        category=category,
        is_read=is_read,
        archived=archived,
    )


@router.patch(
    "/system/notifications/{notification_id}/read",
    response_model=NotificationReadResponse,
)
def mark_notification_read(notification_id: int):
    return ActivityService.mark_notification_read(notification_id)


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


@router.get(
    "/system/resource-task-states/definitions",
    response_model=list[ResourceTaskDefinitionResource],
)
def list_resource_task_state_definitions():
    return ResourceTaskStateService.list_definition_resources()


@router.get(
    "/system/resource-task-states",
    response_model=PageResponse[ResourceTaskRecordResource],
)
def list_resource_task_states(
    task_key: str = Query(...),
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    state: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort: str | None = Query(default=None),
):
    return ResourceTaskStateService.list_record_resources(
        task_key=task_key,
        page=page,
        page_size=page_size,
        state=state,
        search=search,
        sort=sort,
    )


@router.get("/system/events/stream")
def stream_system_events(after_event_id: int = Query(default=0)):
    return StreamingResponse(
        SystemEventService.stream(after_event_id=after_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
