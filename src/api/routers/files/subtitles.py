from fastapi import APIRouter
from fastapi.responses import FileResponse

from src.api.exception.errors import ApiError
from src.common import resolve_subtitle_file_path, verify_subtitle_signature

router = APIRouter(prefix="/files/subtitles", tags=["files"])


@router.get("/{media_id}/{file_name}", include_in_schema=False)
def get_subtitle_file(
    media_id: int,
    file_name: str,
    expires: int | None = None,
    signature: str | None = None,
):
    if expires is None or not signature:
        raise ApiError(403, "file_signature_invalid", "文件签名无效")

    normalized_name = verify_subtitle_signature(media_id, file_name, expires, signature)
    absolute_path = resolve_subtitle_file_path(media_id, normalized_name)
    if not absolute_path.exists() or not absolute_path.is_file():
        raise ApiError(404, "file_not_found", "文件不存在")
    return FileResponse(absolute_path, media_type="text/plain; charset=utf-8")
