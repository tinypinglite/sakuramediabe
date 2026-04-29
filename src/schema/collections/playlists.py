from datetime import datetime
from typing import ClassVar

from pydantic import Field, field_validator

from src.model import PLAYLIST_KIND_CUSTOM
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.base import SchemaModel


class PlaylistResource(SchemaModel):
    SYSTEM_KINDS: ClassVar[set[str]] = {"recently_played"}

    id: int
    name: str
    kind: str = PLAYLIST_KIND_CUSTOM
    description: str = ""
    is_system: bool
    is_mutable: bool
    is_deletable: bool
    movie_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_playlist(cls, playlist, movie_count: int = 0) -> "PlaylistResource":
        is_system = playlist.kind in cls.SYSTEM_KINDS
        return cls.from_peewee_model(
            playlist,
            extra={
                "is_system": is_system,
                "is_mutable": not is_system,
                "is_deletable": not is_system,
                "movie_count": movie_count,
            },
        )


class PlaylistCreateRequest(SchemaModel):
    name: str = Field(min_length=1)
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be blank")
        return value


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
        return value


class PlaylistMovieListItemResource(MovieListItemResource):
    playlist_item_updated_at: datetime
