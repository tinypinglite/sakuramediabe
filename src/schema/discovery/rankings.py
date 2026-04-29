from pydantic import Field

from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


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
