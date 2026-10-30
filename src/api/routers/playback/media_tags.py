from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.playback.media_tags import (
    MediaTagPreviewRequest,
    MediaTagPreviewResponse,
    MediaTagResource,
    MediaTagUpsertRequest,
)
from src.service.playback.media_tag_service import MediaTagService

router = APIRouter(
    prefix="/media-tags",
    tags=["media-tags"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=list[MediaTagResource])
def list_media_tags(
    query: str | None = Query(default=None),
    sort: str | None = Query(default=None),
):
    return MediaTagService.list_tags(query=query, sort=sort)


@router.post(
    "",
    response_model=MediaTagResource,
    status_code=status.HTTP_201_CREATED,
)
def create_media_tag(payload: MediaTagUpsertRequest):
    return MediaTagService.create_tag(payload)


@router.post("/preview", response_model=MediaTagPreviewResponse)
def preview_media_tags(payload: MediaTagPreviewRequest):
    return MediaTagPreviewResponse(
        matches=MediaTagService.preview(
            file_name=payload.file_name,
            release_title=payload.release_title,
            release_description=payload.release_description,
        )
    )


@router.put("/{tag_id}", response_model=MediaTagResource)
def update_media_tag(tag_id: int, payload: MediaTagUpsertRequest):
    return MediaTagService.update_tag(tag_id, payload)


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media_tag(tag_id: int):
    MediaTagService.delete_tag(tag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
