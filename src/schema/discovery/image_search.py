from datetime import datetime

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel


class ImageSearchResultItemResource(SchemaModel):
    thumbnail_id: int
    media_id: int
    movie_id: int
    movie_number: str
    offset_seconds: int
    score: float
    image: ImageResource


class ImageSearchSessionResource(SchemaModel):
    session_id: str
    status: str
    page_size: int
    next_cursor: str | None = None
    expires_at: datetime


class ImageSearchSessionPageResource(ImageSearchSessionResource):
    items: list[ImageSearchResultItemResource]
