from typing import Any

from fastapi import APIRouter, Body, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.config import ConfigResource, ConfigUpdateResource
from src.service.system.config_service import ConfigService

router = APIRouter(
    prefix="/config",
    tags=["config"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=ConfigResource)
def get_config(current_user=Depends(get_current_user)):
    return ConfigService.get_config()


@router.patch("", response_model=ConfigUpdateResource)
def update_config(
    # 嵌套 dict partial，未知 key 由 service 层拒绝。
    payload: dict[str, Any] = Body(...),
    current_user=Depends(get_current_user),
):
    return ConfigService.update_config(payload)
