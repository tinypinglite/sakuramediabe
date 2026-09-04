from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator

from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel
from src.schema.common.playlists import PlaylistSummaryResource


class MovieListStatus(str, Enum):
    ALL = "all"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    PLAYABLE = "playable"


class MovieCollectionType(str, Enum):
    ALL = "all"
    SINGLE = "single"


class TagMatchMode(str, Enum):
    # 多个标签筛选时的组合关系：OR 命中任意一个，AND 须同时包含全部。
    OR = "or"
    AND = "and"


class MovieCollectionMarkType(str, Enum):
    COLLECTION = "collection"
    SINGLE = "single"


class MovieNumberSource(str, Enum):
    # 按番号来源筛选：ALL 不限制，REGULAR 排除 FC2，FC2 仅 FC2（番号以 FC2 开头）。
    ALL = "all"
    REGULAR = "regular"
    FC2 = "fc2"


class MovieReviewSort(str, Enum):
    RECENTLY = "recently"
    HOTLY = "hotly"


class MovieListItemResource(SchemaModel):
    # 影片主键：番号是对外主标识，但统一 action 协议的 resource_ids 收的是整数 id，
    # 所以影片卡片一律带上它，详情页与各列表页都能直接发起资源任务操作。
    id: int
    javdb_id: str | None = None
    metadata_source: dict[str, str | None] | None = None
    movie_number: str
    title: str
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
    is_blacklisted: bool = False
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


class MovieMediaResource(SchemaModel):
    media_id: int = Field(validation_alias="id")
    library_id: int | None = None
    provider_key: str | None = None
    play_url: str
    playback_deliveries: list[Literal["proxy", "redirect"]]
    file_name: str = ""
    resolution: str | None = None
    file_size_bytes: int = 0
    duration_seconds: int = 0
    video_info: dict[str, Any] | None = None
    valid: bool = True
    progress: MovieMediaProgressResource | None = None
    points: list[MovieMediaPointResource] = Field(default_factory=list)


class MovieMergePlaybackCandidateResource(SchemaModel):
    library_id: int
    library_name: str
    provider_key: str
    segment_count: int


class MovieMergedPlaybackResource(SchemaModel):
    play_url: str


class MovieDetailResource(MovieListItemResource):
    actors: list[MovieActorResource]
    tags: list[TagResource]
    summary: str
    maker_name: str | None = None
    director_name: str | None = None
    plot_images: list[ImageResource] = Field(default_factory=list)
    media_items: list[MovieMediaResource] = Field(default_factory=list)
    merge_playback_candidates: list[MovieMergePlaybackCandidateResource] = Field(
        default_factory=list
    )
    playlists: list[PlaylistSummaryResource] = Field(default_factory=list)


class MovieNumberParseRequest(SchemaModel):
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query cannot be blank")
        return normalized


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


class MovieHeatRecomputeParams(SchemaModel):
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


class MovieSubscriptionBatchRequest(SchemaModel):
    movie_numbers: list[str] = Field(min_length=1)

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


class MovieBlacklistBatchRequest(SchemaModel):
    movie_numbers: list[str] = Field(min_length=1, max_length=1000)

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


class MovieSubscriptionSkippedItem(SchemaModel):
    movie_number: str
    reason: str


class MovieSubscriptionBatchResponse(SchemaModel):
    requested_count: int
    updated_count: int
    skipped_count: int = 0
    skipped: list[MovieSubscriptionSkippedItem] = Field(default_factory=list)
