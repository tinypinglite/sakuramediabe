from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


class HotActressResource(SchemaModel):
    id: int
    name: str
    profile_image: ImageResource | None = None
    historical_movie_count: int
    hotness_score: float


class HotActressReleaseMovieResource(MovieListItemResource):
    recommendation_score: float
    hot_actress: HotActressResource
