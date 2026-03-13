from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.indexer_settings import (
    IndexerSettingsResource,
    IndexerSettingsUpdateRequest,
)
from src.service.system.indexer_settings_service import IndexerSettingsService

router = APIRouter(
    prefix="/indexer-settings",
    tags=["indexer-settings"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=IndexerSettingsResource)
def get_indexer_settings(current_user=Depends(get_current_user)):
    return IndexerSettingsService.get_settings()


@router.patch("", response_model=IndexerSettingsResource)
def update_indexer_settings(
    payload: IndexerSettingsUpdateRequest,
    current_user=Depends(get_current_user),
):
    return IndexerSettingsService.update_settings(payload)
