import mimetypes

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import resolve_image_file_path, verify_image_signature
from src.common.image_store import read_image_bytes, thumbnail_pack_path

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
    pack_path = thumbnail_pack_path(normalized_path)
    if pack_path is not None and pack_path.is_file():
        # 缩略图已打包：直接从包内取条目；条目缺失且单文件也不在时按 404 处理。
        try:
            content = read_image_bytes(normalized_path)
        except FileNotFoundError:
            content = None
        if content is not None:
            media_type, _ = mimetypes.guess_type(normalized_path)
            return Response(
                content=content,
                media_type=media_type or "application/octet-stream",
                headers={"Cache-Control": IMAGE_CACHE_CONTROL},
            )
    absolute_path = resolve_image_file_path(normalized_path)
    require_existing_file(absolute_path)
    return FileResponse(absolute_path, headers={"Cache-Control": IMAGE_CACHE_CONTROL})
