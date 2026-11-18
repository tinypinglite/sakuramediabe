from datetime import datetime

from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


class MomentRecommendationItemResource(SchemaModel):
    recommendation_id: int
    rank: int
    score: float
    strategy: str
    reason: str
    media_id: int
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource
    movie: MovieListItemResource


class MomentRecommendationPageResource(SchemaModel):
    items: list[MomentRecommendationItemResource]
    page: int
    page_size: int
    total: int
    generated_at: datetime | None = None
