from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from src.schema.common.base import SchemaModel


class JavdbMovieBaseResource(SchemaModel):
    javdb_id: str = Field()
    movie_number: str
    title: str
    cover_image: Optional[str] = None
    release_date: Optional[str] = None
    duration_minutes: int
    score: Optional[float] = None
    watched_count: int = 0
    want_watch_count: int = 0
    comment_count: int = 0
    score_number: int = 0
    is_subscribed: bool | None = None

    @field_validator("release_date", mode="before")
    @classmethod
    def serialize_release_date(cls, value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value


class JavdbMovieListItemResource(JavdbMovieBaseResource):
    pass


class JavdbMovieActorResource(SchemaModel):
    javdb_id: str = Field()
    javdb_type: int = 0
    name: str
    avatar_url: Optional[str] = Field(default=None)
    gender: int = 0


class JavdbMovieTagResource(SchemaModel):
    javdb_id: str = Field()
    name: str


class JavdbMovieDetailResource(JavdbMovieBaseResource):
    summary: str
    series_name: Optional[str] = Field(default=None)
    actors: List[JavdbMovieActorResource]
    tags: List[JavdbMovieTagResource]
    extra: Optional[Dict[str, Any]] = Field(default=None)
    plot_images: List[str] = Field(
        default_factory=list
    )
