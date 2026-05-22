from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .utils import parse_external_datetime


class ProviderModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class JavdbMovieBase(ProviderModel):
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
        return str(value)


class JavdbMovieListItem(JavdbMovieBase):
    pass


class JavdbMovieActor(ProviderModel):
    javdb_id: str = Field()
    javdb_type: int = 0
    name: str
    alias_names: List[str] = Field(default_factory=list)
    avatar_url: Optional[str] = Field(default=None)
    gender: int = 0


class JavdbSeries(ProviderModel):
    javdb_id: str = Field()
    javdb_type: int = 0
    name: str
    videos_count: int = 0


class JavdbMovieTag(ProviderModel):
    javdb_id: str = Field()
    name: str


class JavdbReviewMovie(ProviderModel):
    id: str = ""
    number: str = ""
    title: str = ""
    origin_title: Optional[str] = None
    score: Optional[float] = None
    thumb_url: Optional[str] = None
    release_date: Optional[str] = None


class JavdbMovieReview(ProviderModel):
    id: int = 0
    score: int = 0
    content: str = ""
    created_at: datetime | None = None
    username: str = ""
    like_count: int = 0
    watch_count: int = 0
    movie: Optional[JavdbReviewMovie] = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, value: Any) -> datetime | None:
        return parse_external_datetime(value)


class JavdbMovieDetail(JavdbMovieBase):
    summary: str
    series_name: Optional[str] = Field(default=None)
    maker_name: Optional[str] = Field(default=None)
    director_name: Optional[str] = Field(default=None)
    actors: List[JavdbMovieActor]
    tags: List[JavdbMovieTag]
    extra: Optional[Dict[str, Any]] = Field(default=None)
    plot_images: List[str] = Field(default_factory=list)


class MissavThumbnailManifest(ProviderModel):
    movie_number: str
    page_url: str
    pic_num: int
    width: int
    height: int
    col: int
    row: int
    offset_x: int
    offset_y: int
    urls: list[str]

    @classmethod
    def from_dict(cls, payload: dict) -> "MissavThumbnailManifest":
        return cls.model_validate(payload)


JavdbMovieBaseResource = JavdbMovieBase
JavdbMovieListItemResource = JavdbMovieListItem
JavdbMovieActorResource = JavdbMovieActor
JavdbSeriesResource = JavdbSeries
JavdbMovieTagResource = JavdbMovieTag
JavdbReviewMovieResource = JavdbReviewMovie
JavdbMovieReviewResource = JavdbMovieReview
JavdbMovieDetailResource = JavdbMovieDetail
