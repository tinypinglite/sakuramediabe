from fastapi import APIRouter, Depends, Query, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.playback.cloud115_libraries import (
    Cloud115BrowseResponse,
    Cloud115LibraryCreateRequest,
    Cloud115LibraryReauthRequest,
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


@router.post(
    "/{library_id}/reauth",
    response_model=MediaLibraryResource,
)
async def reauth_cloud115_library(
    library_id: int,
    payload: Cloud115LibraryReauthRequest,
    current_user=Depends(get_current_user),
):
    """扫码 CONFIRMED 后，为已有媒体库更新 cookies；禁止切换到其它 115 账号。"""
    return await MediaLibraryService.reauth_cloud115_library(library_id, payload)


@router.get("/{library_id}/entries", response_model=Cloud115BrowseResponse)
async def browse_cloud115_directory(
    library_id: int,
    cid: str = Query(default="0"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1150),
    current_user=Depends(get_current_user),
):
    """列 115 目录一页（前端目录选择器选导入源用）。响应带 root_cid 供前端禁选管理目录。"""
    return await MediaLibraryService.browse_cloud115_directory(
        library_id, cid=cid, offset=offset, limit=limit
    )
