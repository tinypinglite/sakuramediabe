from fastapi import APIRouter
from fastapi.responses import FileResponse

from src.api.routers._utils import require_existing_file, require_signed_params
from src.common import resolve_subtitle_file_path, verify_subtitle_signature

router = APIRouter(prefix="/files/subtitles", tags=["files"])


@router.get("/{subtitle_id}", include_in_schema=False)
def get_subtitle_file(
    subtitle_id: int,
    expires: int | None = None,
    signature: str | None = None,
):
    require_signed_params(expires, signature)

    verify_subtitle_signature(subtitle_id, expires, signature)
    absolute_path = resolve_subtitle_file_path(subtitle_id)
    require_existing_file(absolute_path)
    return FileResponse(absolute_path, media_type="text/plain; charset=utf-8")
