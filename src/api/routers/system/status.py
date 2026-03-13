from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.status import StatusResource
from src.service.system.status_service import StatusService

router = APIRouter(
    tags=["status"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("/status", response_model=StatusResource)
def get_status():
    return StatusService.get_status()
