from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.movie_desc_translation_settings import (
    MovieDescTranslationSettingsResource,
    MovieDescTranslationSettingsTestRequest,
    MovieDescTranslationSettingsTestResource,
    MovieDescTranslationSettingsUpdateRequest,
)
from src.service.system.movie_desc_translation_settings_service import (
    MovieDescTranslationSettingsService,
)

router = APIRouter(
    prefix="/movie-desc-translation-settings",
    # 路径保持兼容，文档分组统一按“影片信息翻译配置”命名。
    tags=["movie-info-translation-settings"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=MovieDescTranslationSettingsResource)
def get_movie_desc_translation_settings(current_user=Depends(get_current_user)):
    return MovieDescTranslationSettingsService.get_settings()


@router.patch("", response_model=MovieDescTranslationSettingsResource)
def update_movie_desc_translation_settings(
    payload: MovieDescTranslationSettingsUpdateRequest,
    current_user=Depends(get_current_user),
):
    return MovieDescTranslationSettingsService.update_settings(payload)


@router.post("/test", response_model=MovieDescTranslationSettingsTestResource)
def test_movie_desc_translation_settings(
    payload: MovieDescTranslationSettingsTestRequest,
    current_user=Depends(get_current_user),
):
    return MovieDescTranslationSettingsService.test_settings(payload)
