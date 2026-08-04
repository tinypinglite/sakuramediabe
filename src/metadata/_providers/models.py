from __future__ import annotations

from datetime import date, datetime
from typing import Any

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
    cover_image: str | None = None
    release_date: str | None = None
    duration_minutes: int
    score: float | None = None
    watched_count: int = 0
    want_watch_count: int = 0
    comment_count: int = 0
    score_number: int = 0
    is_subscribed: bool | None = None

    @field_validator("release_date", mode="before")
    @classmethod
    def serialize_release_date(cls, value: Any) -> str | None:
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
    alias_names: list[str] = Field(default_factory=list)
    avatar_url: str | None = Field(default=None)
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
    origin_title: str | None = None
    score: float | None = None
    thumb_url: str | None = None
    release_date: str | None = None


class JavdbMovieReview(ProviderModel):
    id: int = 0
    score: int = 0
    content: str = ""
    created_at: datetime | None = None
    username: str = ""
    like_count: int = 0
    watch_count: int = 0
    movie: JavdbReviewMovie | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, value: Any) -> datetime | None:
        return parse_external_datetime(value)


class JavdbMovieDetail(JavdbMovieBase):
    summary: str
    series_name: str | None = Field(default=None)
    maker_name: str | None = Field(default=None)
    director_name: str | None = Field(default=None)
    actors: list[JavdbMovieActor]
    tags: list[JavdbMovieTag]
    extra: dict[str, Any] | None = Field(default=None)
    plot_images: list[str] = Field(default_factory=list)


JavdbMovieListItemResource = JavdbMovieListItem
JavdbMovieActorResource = JavdbMovieActor
JavdbSeriesResource = JavdbSeries
JavdbMovieTagResource = JavdbMovieTag
JavdbReviewMovieResource = JavdbReviewMovie
JavdbMovieReviewResource = JavdbMovieReview
JavdbMovieDetailResource = JavdbMovieDetail
