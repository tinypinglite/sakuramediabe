from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator

from src.i18n.playlist_names import (
    localize_system_playlist_description,
    localize_system_playlist_name,
)
from src.model import PLAYLIST_KIND_CUSTOM, SYSTEM_PLAYLIST_KINDS
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel
from src.schema.common.batch import MAX_BATCH_ITEMS


class PlaylistResource(SchemaModel):
    SYSTEM_KINDS: ClassVar[set[str]] = set(SYSTEM_PLAYLIST_KINDS)

    id: int
    name: str
    kind: str = PLAYLIST_KIND_CUSTOM
    description: str = ""
    is_system: bool
    # 仅控制列表信息（改名/描述）是否可编辑；收藏的成员增删按 kind 单独判定。
    is_mutable: bool
    is_deletable: bool
    movie_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_playlist(cls, playlist, movie_count: int = 0) -> "PlaylistResource":
        is_system = playlist.kind in cls.SYSTEM_KINDS
        extra: dict = {
            "is_system": is_system,
            "is_mutable": not is_system,
            "is_deletable": not is_system,
            "movie_count": movie_count,
        }
        if is_system:
            # 系统列表的名字按请求语言替换；自定义列表是用户数据，保持原样。
            extra["name"] = localize_system_playlist_name(playlist.kind, playlist.name)
            extra["description"] = localize_system_playlist_description(
                playlist.kind,
                playlist.description,
            )
        return cls.from_peewee_model(playlist, extra=extra)


class PlaylistCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class PlaylistUpdateRequest(SchemaModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return normalized


class PlaylistMovieListItemResource(MovieListItemResource):
    playlist_item_updated_at: datetime


class PlaylistMovieBatchAddRequest(SchemaModel):
    movie_numbers: list[str] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @field_validator("movie_numbers")
    @classmethod
    def validate_movie_numbers(cls, value: list[str]) -> list[str]:
        normalized_numbers: list[str] = []
        seen: set[str] = set()
        for movie_number in value:
            normalized = (movie_number or "").strip()
            if not normalized:
                raise ValueError("movie_numbers item cannot be blank")
            if normalized in seen:
                raise ValueError("movie_numbers 不可重复")
            seen.add(normalized)
            normalized_numbers.append(normalized)
        return normalized_numbers


class PlaylistMovieBatchAddSkippedItem(SchemaModel):
    movie_number: str
    reason: str


class PlaylistMovieBatchAddResponse(SchemaModel):
    requested_count: int
    updated_count: int
    skipped_count: int = 0
    skipped: list[PlaylistMovieBatchAddSkippedItem] = Field(default_factory=list)


class PlaylistResolutionOption(SchemaModel):
    # 播放列表内影片覆盖的分辨率档位（归一化标签）及命中影片数，供前端渲染筛选下拉。
    resolution: str
    count: int
