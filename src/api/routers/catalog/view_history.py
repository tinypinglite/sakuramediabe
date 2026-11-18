from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.model import VIEW_HISTORY_MAX_ENTRIES
from src.schema.catalog.view_history import (
    ViewHistoryListResource,
    ViewHistoryRecordRequest,
)
from src.service.catalog import ViewHistoryService

router = APIRouter(
    prefix="/view-history",
    tags=["view-history"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
def record_view_history(payload: ViewHistoryRecordRequest):
    ViewHistoryService.record(
        entity_type=payload.entity_type.value,
        entity_id=payload.entity_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "",
    response_model=ViewHistoryListResource,
    response_model_by_alias=False,
)
def list_view_history(
    limit: int = Query(default=20, ge=1, le=VIEW_HISTORY_MAX_ENTRIES),
):
    return ViewHistoryService.list_entries(limit=limit)
