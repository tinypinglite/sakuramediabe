from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.playback.cloud115_libraries import (
    Cloud115LibraryCreateRequest,
    Cloud115QrStatusRequest,
    Cloud115QrStatusResource,
    Cloud115QrTokenResource,
)
from src.schema.playback.media_libraries import MediaLibraryResource
from src.service.playback import Cloud115QrLoginService, MediaLibraryService

router = APIRouter(
    prefix="/media-libraries/cloud115",
    tags=["media-libraries"],
    dependencies=[Depends(db_deps)],
)


@router.post("/qrlogin/token", response_model=Cloud115QrTokenResource)
async def get_qrlogin_token(current_user=Depends(get_current_user)):
    """建一次扫码会话，返回 uid/time/sign + 二维码 PNG (base64)。"""
    return await Cloud115QrLoginService.get_token()


@router.post("/qrlogin/status", response_model=Cloud115QrStatusResource)
async def poll_qrlogin_status(
    payload: Cloud115QrStatusRequest,
    current_user=Depends(get_current_user),
):
    """长轮询扫码状态（阻塞 ~30s）。上层拿到 waiting/scanned 继续 poll，confirmed 进创建。"""
    return await Cloud115QrLoginService.poll_status(payload)


@router.post(
    "",
    response_model=MediaLibraryResource,
    status_code=status.HTTP_201_CREATED,
)
async def create_cloud115_library(
    payload: Cloud115LibraryCreateRequest,
    current_user=Depends(get_current_user),
):
    """扫码 CONFIRMED 后：换 cookies → 校验 alive → find-or-create sakuramedia/ → 落库。"""
    return await MediaLibraryService.create_cloud115_library(payload)
