from enum import Enum
from datetime import date, datetime
from typing import Any, List

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


class MovieCollectionMarkType(str, Enum):
    COLLECTION = "collection"
    SINGLE = "single"


class MovieSpecialTagFilter(str, Enum):
    FOUR_K = "4k"
    UNCENSORED = "uncensored"
    VR = "vr"

    def to_media_tag(self) -> str:
        if self == MovieSpecialTagFilter.FOUR_K:
            return "4K"
        if self == MovieSpecialTagFilter.UNCENSORED:
            return "无码"
        return "VR"


class MovieReviewSort(str, Enum):
    RECENTLY = "recently"
    HOTLY = "hotly"


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
    title_zh: str = ""
    series_id: int | None = None
    series_name: str | None = None
    cover_image: ImageResource | None = None
    thin_cover_image: ImageResource | None = None
    release_date: str | None = None
    duration_minutes: int
    score: float = 0.0
    watched_count: int = 0
    want_watch_count: int = 0
    comment_count: int = 0
    score_number: int = 0
    heat: int = 0
    is_collection: bool
    is_subscribed: bool
    can_play: bool = False
    is_4k: bool = False

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


class SimilarMovieListItemResource(MovieListItemResource):
    similarity_score: float = 0.0


class MovieActorResource(SchemaModel):
    id: int
    javdb_id: str = Field()
    name: str
    alias_name: str = Field()
    gender: int
    is_subscribed: bool = Field()
    profile_image: ImageResource | None = None


class TagResource(SchemaModel):
    tag_id: int
    name: str


class TagListItemResource(SchemaModel):
    tag_id: int = Field(validation_alias="id")
    name: str
    movie_count: int = 0


class MovieMediaProgressResource(SchemaModel):
    last_position_seconds: int
    last_watched_at: datetime | None = None


class MovieMediaPointResource(SchemaModel):
    point_id: int
    thumbnail_id: int
    offset_seconds: int
    image: ImageResource


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
    video_info: dict[str, Any] | None = None
    special_tags: str = "普通"
    valid: bool = True
    progress: MovieMediaProgressResource | None = None
    points: List[MovieMediaPointResource] = Field(default_factory=list)


class MovieDetailResource(MovieListItemResource):
    actors: List[MovieActorResource]
    tags: List[TagResource]
    summary: str
    desc: str = ""
    desc_zh: str = ""
    maker_name: str | None = None
    director_name: str | None = None
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


class MovieSeriesListRequest(SchemaModel):
    series_id: int = Field(ge=1)
    sort: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class MovieSeriesJavdbImportStatsResource(SchemaModel):
    total: int = 0
    created_count: int = 0
    already_exists_count: int = 0
    failed_count: int = 0


class MovieSeriesJavdbImportCompletedResource(SchemaModel):
    success: bool
    movies: List[MovieListItemResource] = Field(default_factory=list)
    skipped_items: List[dict[str, Any]] = Field(default_factory=list)
    failed_items: List[dict[str, Any]] = Field(default_factory=list)
    stats: MovieSeriesJavdbImportStatsResource | None = None
    reason: str | None = None


class MovieCollectionMarkRequest(SchemaModel):
    movie_numbers: list[str] = Field(min_length=1)
    collection_type: MovieCollectionMarkType

    @field_validator("movie_numbers")
    @classmethod
    def validate_movie_numbers(cls, value: list[str]) -> list[str]:
        validated_numbers: list[str] = []
        for movie_number in value:
            normalized = (movie_number or "").strip()
            if not normalized:
                raise ValueError("movie_numbers item cannot be blank")
            validated_numbers.append(normalized)
        return validated_numbers


class MovieCollectionMarkResponse(SchemaModel):
    requested_count: int
    updated_count: int


class MovieCollectionStatusResource(SchemaModel):
    movie_number: str
    is_collection: bool


class MissavThumbnailItemResource(SchemaModel):
    index: int
    url: str


class MissavThumbnailResource(SchemaModel):
    movie_number: str
    source: str
    total: int
    items: List[MissavThumbnailItemResource] = Field(default_factory=list)
