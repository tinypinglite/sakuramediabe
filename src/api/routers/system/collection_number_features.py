from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.collection_number_features import (
    CollectionNumberFeaturesResource,
    CollectionNumberFeaturesUpdateRequest,
)
from src.service.system.collection_number_features_service import (
    CollectionNumberFeaturesService,
)

router = APIRouter(
    prefix="/collection-number-features",
    tags=["collection-number-features"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=CollectionNumberFeaturesResource)
def get_collection_number_features(current_user=Depends(get_current_user)):
    return CollectionNumberFeaturesService.get_features()


@router.patch("", response_model=CollectionNumberFeaturesResource)
def update_collection_number_features(
    payload: CollectionNumberFeaturesUpdateRequest,
    apply_now: bool = Query(default=True),
    current_user=Depends(get_current_user),
):
    return CollectionNumberFeaturesService.update_features(payload, apply_now=apply_now)
