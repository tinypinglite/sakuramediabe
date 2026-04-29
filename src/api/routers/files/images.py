from fastapi import APIRouter
from fastapi.responses import FileResponse

from src.api.exception.errors import ApiError
from src.common import resolve_image_file_path, verify_image_signature

router = APIRouter(prefix="/files/images", tags=["files"])


@router.get("/{file_path:path}", include_in_schema=False)
def get_image_file(
    file_path: str,
    expires: int | None = None,
    signature: str | None = None,
):
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    normalized_path = verify_image_signature(file_path, expires, signature)
    absolute_path = resolve_image_file_path(normalized_path)
    if not absolute_path.exists() or not absolute_path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")
    return FileResponse(absolute_path)
