from datetime import datetime

from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel
from src.schema.common.pagination import PageResponse


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


class HotReviewListResource(PageResponse[HotReviewListItemResource]):
    # 该周期热评整批的抓取时间（整周期删旧插新，全批一致）
    synced_at: datetime | None = None
