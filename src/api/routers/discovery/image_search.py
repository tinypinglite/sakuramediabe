from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile

from src.api.routers.deps import db_deps, get_current_user
from src.schema.discovery import ImageSearchSessionPageResource, ImageSearchSessionResource
from src.service.discovery import get_image_search_service

router = APIRouter(
    prefix="/image-search",
    tags=["image-search"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _parse_csv_ints(raw: str | None, field_name: str) -> list[int] | None:
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return None
    try:
        return [int(item) for item in parts]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} format") from exc


@router.post("/sessions", response_model=ImageSearchSessionPageResource)
async def create_image_search_session(
    file: Annotated[UploadFile, File(...)],
    page_size: Annotated[int | None, Form()] = None,
    movie_ids: Annotated[str | None, Form()] = None,
    exclude_movie_ids: Annotated[str | None, Form()] = None,
    score_threshold: Annotated[float | None, Form()] = None,
):
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    service = get_image_search_service()
    try:
        return service.create_session_and_first_page(
            image_bytes=image_bytes,
            page_size=page_size,
            movie_ids=_parse_csv_ints(movie_ids, "movie_ids"),
            exclude_movie_ids=_parse_csv_ints(exclude_movie_ids, "exclude_movie_ids"),
            score_threshold=score_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sessions/{session_id}", response_model=ImageSearchSessionResource)
def get_image_search_session(
    session_id: Annotated[str, Path(min_length=1)],
):
    service = get_image_search_service()
    try:
        return service.get_session(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/results", response_model=ImageSearchSessionPageResource)
def get_image_search_results(
    session_id: Annotated[str, Path(min_length=1)],
    cursor: Annotated[str | None, Query(min_length=1)] = None,
):
    service = get_image_search_service()
    try:
        return service.list_results(session_id, cursor=cursor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
