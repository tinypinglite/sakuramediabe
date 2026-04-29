from datetime import datetime

from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


class HotReviewListItemResource(SchemaModel):
    rank: int
    review_id: int
    score: int
    content: str
    created_at: datetime | None = None
    username: str
    like_count: int
    watch_count: int
    movie: MovieListItemResource
