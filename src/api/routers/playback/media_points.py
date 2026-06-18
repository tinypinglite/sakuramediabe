from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import MediaPointKind, MediaPointListItemResource
from src.service.playback import MediaService

router = APIRouter(
    tags=["media"],
    dependencies=[Depends(db_deps)],
)


@router.get("/media-points", response_model=PageResponse[MediaPointListItemResource])
def list_media_points(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    sort: str | None = Query(default=None),
    # 默认只返回 JAV 时刻，前端可按 video/all 切换查看非 JAV 或全部。
    kind: MediaPointKind = Query(default=MediaPointKind.JAV),
    current_user=Depends(get_current_user),
):
    return MediaService.list_media_points(page=page, page_size=page_size, sort=sort, kind=kind)
