from datetime import datetime
from enum import Enum

from src.schema.catalog.actors import ActorResource
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


class ViewHistoryEntityType(str, Enum):
    MOVIE = "movie"
    ACTOR = "actor"


class ViewHistoryRecordRequest(SchemaModel):
    entity_type: ViewHistoryEntityType
    entity_id: int


class ViewHistoryEntryResource(SchemaModel):
    entity_type: ViewHistoryEntityType
    viewed_at: datetime
    movie: MovieListItemResource | None = None
    actor: ActorResource | None = None


class ViewHistoryListResource(SchemaModel):
    items: list[ViewHistoryEntryResource]
