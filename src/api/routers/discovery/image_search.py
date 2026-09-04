from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    UploadFile,
)

from src.api.routers._utils import parse_csv_positive_ints
from src.api.routers.deps import db_deps, get_current_user
from src.schema.discovery import (
    ImageSearchSessionPageResource,
    MoviePlotImageSearchSessionPageResource,
)
from src.service.discovery import (
    get_image_search_service,
    get_movie_plot_image_search_service,
)
from src.service.discovery.image_search_input import normalize_image_search_query

router = APIRouter(
    prefix="/image-search",
    tags=["image-search"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post("/sessions", response_model=ImageSearchSessionPageResource)
async def create_image_search_session(
    file: Annotated[UploadFile, File(...)],
    page_size: Annotated[int | None, Form()] = None,
    movie_ids: Annotated[str | None, Form()] = None,
    exclude_movie_ids: Annotated[str | None, Form()] = None,
    score_threshold: Annotated[float | None, Form()] = None,
):
    service = get_image_search_service()
    try:
        image_bytes = await _read_image_search_query(file)
        return service.create_session_and_first_page(
            image_bytes=image_bytes,
            page_size=page_size,
            movie_ids=parse_csv_positive_ints(
                movie_ids, "movie_ids", error_code="invalid_image_search_filter"
            ),
            exclude_movie_ids=parse_csv_positive_ints(
                exclude_movie_ids, "exclude_movie_ids", error_code="invalid_image_search_filter"
            ),
            score_threshold=score_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@router.post("/text-sessions", response_model=ImageSearchSessionPageResource)
def create_text_image_search_session(
    text: Annotated[str, Form(min_length=1)],
    page_size: Annotated[int | None, Form()] = None,
    movie_ids: Annotated[str | None, Form()] = None,
    exclude_movie_ids: Annotated[str | None, Form()] = None,
    score_threshold: Annotated[float | None, Form()] = None,
):
    try:
        return get_image_search_service().create_text_session_and_first_page(
            text=text,
            page_size=page_size,
            movie_ids=parse_csv_positive_ints(movie_ids, "movie_ids", error_code="invalid_image_search_filter"),
            exclude_movie_ids=parse_csv_positive_ints(exclude_movie_ids, "exclude_movie_ids", error_code="invalid_image_search_filter"),
            score_threshold=score_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plot-sessions", response_model=MoviePlotImageSearchSessionPageResource)
async def create_plot_image_search_session(
    file: Annotated[UploadFile, File(...)],
    page_size: Annotated[int | None, Form()] = None,
    movie_ids: Annotated[str | None, Form()] = None,
    exclude_movie_ids: Annotated[str | None, Form()] = None,
    score_threshold: Annotated[float | None, Form()] = None,
):
    try:
        image_bytes = await _read_image_search_query(file)
        return get_movie_plot_image_search_service().create_session_and_first_page(
            image_bytes=image_bytes,
            page_size=page_size,
            movie_ids=parse_csv_positive_ints(
                movie_ids, "movie_ids", error_code="invalid_image_search_filter"
            ),
            exclude_movie_ids=parse_csv_positive_ints(
                exclude_movie_ids,
                "exclude_movie_ids",
                error_code="invalid_image_search_filter",
            ),
            score_threshold=score_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _read_image_search_query(file: UploadFile) -> bytes:
    image_bytes = await file.read()
    if not image_bytes:
        raise ValueError("Uploaded file is empty")
    return normalize_image_search_query(image_bytes)


@router.get(
    "/plot-sessions/{session_id}/results",
    response_model=MoviePlotImageSearchSessionPageResource,
)
def get_plot_image_search_results(
    session_id: Annotated[str, Path(min_length=1)],
    cursor: Annotated[str | None, Query(min_length=1)] = None,
):
    try:
        return get_movie_plot_image_search_service().list_results(
            session_id, cursor=cursor
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/plot-text-sessions", response_model=MoviePlotImageSearchSessionPageResource)
def create_plot_text_search_session(
    text: Annotated[str, Form(min_length=1)],
    page_size: Annotated[int | None, Form()] = None,
    movie_ids: Annotated[str | None, Form()] = None,
    exclude_movie_ids: Annotated[str | None, Form()] = None,
    score_threshold: Annotated[float | None, Form()] = None,
):
    try:
        return get_movie_plot_image_search_service().create_text_session_and_first_page(
            text=text,
            page_size=page_size,
            movie_ids=parse_csv_positive_ints(movie_ids, "movie_ids", error_code="invalid_image_search_filter"),
            exclude_movie_ids=parse_csv_positive_ints(exclude_movie_ids, "exclude_movie_ids", error_code="invalid_image_search_filter"),
            score_threshold=score_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
