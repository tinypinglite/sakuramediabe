from datetime import datetime

from pydantic import Field

from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel
from src.schema.common.pagination import PageResponse


class RankingSourceResource(SchemaModel):
    source_key: str = Field(min_length=1)
    name: str = Field(min_length=1)


class RankingBoardResource(SchemaModel):
    source_key: str = Field(min_length=1)
    board_key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    supported_periods: list[str] = Field(default_factory=list)
    default_period: str | None = None


class RankedMovieListItemResource(MovieListItemResource):
    rank: int


class RankingBoardItemsResource(PageResponse[RankedMovieListItemResource]):
    # 该榜单+周期整批的抓取时间（整榜删旧插新，全批一致）
    synced_at: datetime | None = None
