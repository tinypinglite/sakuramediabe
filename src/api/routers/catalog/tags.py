from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.movies import (
    MovieCollectionType,
    MovieListItemResource,
    MovieListStatus,
    MovieSpecialTagFilter,
    TagListItemResource,
)
from src.schema.common.pagination import PageResponse
from src.service.catalog import TagService

router = APIRouter(
    prefix="/tags",
    tags=["tags"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _parse_optional_exact_text(raw: str | None, field_name: str) -> str | None:
    if raw is None:
        return None

    # 影片维度的精确筛选不接受空白值，避免客户端误以为空串会被忽略。
    normalized = raw.strip()
    if not normalized:
        raise ApiError(
            422,
            "invalid_movie_filter",
            "Invalid filter value",
            {field_name: raw},
        )
    return normalized


@router.get("", response_model=List[TagListItemResource], response_model_by_alias=False)
def list_tags(
    query: str | None = Query(default=None),
    sort: Optional[str] = Query(default=None),
):
    return TagService.list_tags(query=query, sort=sort)


@router.get("/{tag_id}", response_model=TagListItemResource, response_model_by_alias=False)
def get_tag(tag_id: int):
    return TagService.get_tag(tag_id)


@router.get("/{tag_id}/movies", response_model=PageResponse[MovieListItemResource])
def list_tag_movies(
    tag_id: int,
    year: int | None = Query(default=None, ge=1),
    status: MovieListStatus = MovieListStatus.ALL,
    collection_type: MovieCollectionType = MovieCollectionType.ALL,
    special_tag: MovieSpecialTagFilter | None = None,
    sort: Optional[str] = Query(default=None),
    director_name: str | None = Query(default=None),
    maker_name: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return TagService.list_tag_movies(
        tag_id=tag_id,
        year=year,
        status=status,
        collection_type=collection_type,
        special_tag=special_tag,
        sort=sort,
        director_name=_parse_optional_exact_text(director_name, "director_name"),
        maker_name=_parse_optional_exact_text(maker_name, "maker_name"),
        page=page,
        page_size=page_size,
    )
