from datetime import datetime

from src.schema.catalog.actors import ActorResource
from src.schema.common.base import SchemaModel


class TasteProfileActressResource(SchemaModel):
    actor: ActorResource
    matched_movie_count: int
    score: float


class TasteProfileTagResource(SchemaModel):
    tag_id: int
    name: str
    matched_movie_count: int
    score: float


class TasteProfileResource(SchemaModel):
    sample_movie_count: int
    generated_at: datetime
    actresses: list[TasteProfileActressResource]
    unsubscribed_actresses: list[TasteProfileActressResource]
    tags: list[TasteProfileTagResource]
