from fastapi import APIRouter
from fastapi.responses import FileResponse

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import resolve_image_file_path, verify_image_signature

router = APIRouter(prefix="/files/images", tags=["files"])

# 图片按路径不可变（内容变更走新路径），给客户端一个与签名轮换无关的长缓存窗口。
IMAGE_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
IMAGE_CACHE_CONTROL = f"public, max-age={IMAGE_CACHE_MAX_AGE_SECONDS}, immutable"


@router.get("/{file_path:path}", include_in_schema=False)
def get_image_file(
    file_path: str,
    expires: int | None = None,
    signature: str | None = None,
):
    require_signed_params(expires, signature)

    normalized_path = verify_image_signature(file_path, expires, signature)
    absolute_path = resolve_image_file_path(normalized_path)
    require_existing_file(absolute_path)
    return FileResponse(absolute_path, headers={"Cache-Control": IMAGE_CACHE_CONTROL})
