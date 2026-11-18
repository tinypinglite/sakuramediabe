from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.discovery import TasteProfileResource
from src.service.discovery import TasteProfileService

router = APIRouter(
    prefix="/taste-profile",
    tags=["taste-profile"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=TasteProfileResource)
def get_taste_profile():
    return TasteProfileService.get_profile()
