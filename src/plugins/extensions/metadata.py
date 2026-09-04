"""按番号交付完整元数据；图片由插件下载，宿主接管后入库。"""

from collections.abc import Callable
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.plugins.contracts import PluginExtension

METADATA_SOURCE_EXTENSION_KEY = "catalog.metadata_source"
METADATA_SOURCE_HOST_API_VERSION = 6


class PluginMetadataActor(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    name: str = Field(min_length=1)
    alias_names: list[str] = Field(default_factory=list)


class PluginMovieMetadata(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    movie_number: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1)
    release_date: date
    duration_minutes: int = Field(gt=0, strict=True)
    cover_image_path: str = Field(min_length=1)
    summary: str = ""
    maker_name: str | None = Field(default=None, max_length=255)
    director_name: str | None = Field(default=None, max_length=255)
    series_name: str | None = Field(default=None, max_length=255)
    actors: list[PluginMetadataActor] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    plot_image_paths: list[str] = Field(default_factory=list)
    source_url: str | None = None
    source_id: str | None = None

    @field_validator("release_date", mode="before")
    @classmethod
    def validate_date(cls, value):
        if isinstance(value, str):
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        elif type(value) is date:
            return value
        raise ValueError("release_date 必须是 YYYY-MM-DD 日期")

    @field_validator("actors", "tags", "plot_image_paths", mode="before")
    @classmethod
    def empty_lists(cls, value):
        return [] if value is None else value

    @field_validator("summary", mode="before")
    @classmethod
    def empty_summary(cls, value):
        return "" if value is None else value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value):
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))


class PluginMetadataSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    fetch_movie: Callable[[str], PluginMovieMetadata | None]


def validate_metadata_extension(
    *, plugin_id: str, extension: PluginExtension
) -> PluginMetadataSource:
    return PluginMetadataSource.model_validate(extension.data)
