from typing import Optional

from fastapi import APIRouter, Depends, Query

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.discovery import (
    RankedMovieListItemResource,
    RankingBoardResource,
    RankingSourceResource,
)
from src.service.discovery import RankingCatalogService

router = APIRouter(
    prefix="/ranking-sources",
    tags=["ranking-sources"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=list[RankingSourceResource])
def list_ranking_sources():
    return RankingCatalogService.list_sources()


@router.get("/{source_key}/boards", response_model=list[RankingBoardResource])
def list_ranking_boards(source_key: str):
    return RankingCatalogService.list_boards(source_key)


@router.get(
    "/{source_key}/boards/{board_key}/items",
    response_model=PageResponse[RankedMovieListItemResource],
)
def list_ranking_board_items(
    source_key: str,
    board_key: str,
    period: Optional[str] = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return RankingCatalogService.list_board_items(
        source_key=source_key,
        board_key=board_key,
        period=period,
        page=page,
        page_size=page_size,
    )
