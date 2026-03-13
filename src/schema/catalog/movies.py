from enum import Enum
from datetime import date, datetime
from typing import List

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.common.playlists import PlaylistSummaryResource


class MovieListStatus(str, Enum):
    ALL = "all"
    SUBSCRIBED = "subscribed"
    PLAYABLE = "playable"


class MovieCollectionType(str, Enum):
    ALL = "all"
    SINGLE = "single"


MOVIE_LIST_SORT_FIELDS = (
    "release_date",
    "added_at",
    "subscribed_at",
    "comment_count",
    "score_number",
    "want_watch_count",
    "heat",
)


class MovieListItemResource(SchemaModel):
    javdb_id: str = Field()
    movie_number: str
    title: str
    series_name: str | None = None
    cover_image: ImageResource | None = None
    release_date: str | None = None
    duration_minutes: int
    score: float = 0.0
    watched_count: int = 0
    want_watch_count: int = 0
    comment_count: int = 0
    score_number: int = 0
    is_collection: bool
    is_subscribed: bool
    can_play: bool = False

    @field_validator("release_date", mode="before")
    @classmethod
    def serialize_release_date(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value


class ActorMovieResource(SchemaModel):
    javdb_id: str = Field()
    movie_number: str
    title: str
    cover_image: ImageResource | None = None
    can_play: bool = False


class MovieActorResource(SchemaModel):
    id: int
    javdb_id: str = Field()
    name: str
    alias_name: str = Field()
    is_subscribed: bool = Field()
    profile_image: ImageResource | None = None


class TagResource(SchemaModel):
    tag_id: int
    name: str


class MovieMediaProgressResource(SchemaModel):
    last_position_seconds: int
    last_watched_at: datetime | None = None


class MovieMediaPointResource(SchemaModel):
    point_id: int
    offset_seconds: int


class MovieMediaSubtitleResource(SchemaModel):
    file_name: str
    url: str


class MovieMediaResource(SchemaModel):
    media_id: int = Field(validation_alias="id")
    library_id: int | None = None
    play_url: str
    storage_mode: str | None = None
    resolution: str | None = None
    file_size_bytes: int = 0
    duration_seconds: int = 0
    special_tags: str = "普通"
    valid: bool = True
    progress: MovieMediaProgressResource | None = None
    points: List[MovieMediaPointResource] = Field(default_factory=list)
    subtitles: List[MovieMediaSubtitleResource] = Field(default_factory=list)


class MovieDetailResource(MovieListItemResource):
    actors: List[MovieActorResource]
    tags: List[TagResource]
    summary: str
    thin_cover_image: ImageResource | None = None
    plot_images: List[ImageResource] = Field(default_factory=list)
    media_items: List[MovieMediaResource] = Field(default_factory=list)
    playlists: List[PlaylistSummaryResource] = Field(default_factory=list)


class MovieNumberParseRequest(SchemaModel):
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query cannot be blank")
        return value


class MovieNumberParseResponse(SchemaModel):
    query: str
    parsed: bool
    movie_number: str | None = None
    reason: str | None = None


class MovieJavdbSearchRequest(SchemaModel):
    movie_number: str = Field(min_length=1)

    @field_validator("movie_number")
    @classmethod
    def validate_movie_number(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("movie_number cannot be blank")
        return normalized
