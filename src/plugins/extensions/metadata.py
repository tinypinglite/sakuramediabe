"""按番号交付完整元数据；可选提供演员影片列表；图片由插件下载，宿主接管后入库。"""

from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.plugins.contracts import PluginExtension

METADATA_SOURCE_EXTENSION_KEY = "catalog.metadata_source"
METADATA_SOURCE_HOST_API_VERSION = 18


class PluginMetadataActor(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    name: str = Field(min_length=1)
    alias_names: list[str] = Field(default_factory=list)
    # 0 未知、1 女、2 男；插件不知道时保持未知，宿主不猜测。
    gender: int = Field(default=0, ge=0, le=2, strict=True)


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


class PluginActorRef(BaseModel):
    """宿主传给插件的最小演员身份；外部来源 ID 由插件自行缓存。"""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    actor_id: int = Field(gt=0, strict=True)
    name: str = Field(min_length=1, max_length=255)
    alias_names: list[str] = Field(default_factory=list)
    # 0 未知、1 女、2 男；插件可按来源能力决定是否处理。
    gender: int = Field(default=0, ge=0, le=2, strict=True)

    @field_validator("alias_names", mode="before")
    @classmethod
    def empty_alias_names(cls, value):
        return [] if value is None else value

    @field_validator("alias_names")
    @classmethod
    def clean_alias_names(cls, value):
        names: list[str] = []
        seen: set[str] = set()
        for name in value:
            normalized = (name or "").strip()
            key = normalized.casefold()
            if not normalized or key in seen:
                continue
            seen.add(key)
            names.append(normalized)
        return names


class PluginActorMovieQuery(BaseModel):
    """演员影片列表的一页请求；游标只在本次同步内有效。"""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    actor: PluginActorRef
    cursor: str | None = Field(default=None, max_length=512)
    # incremental 由插件自行决定增量范围；full 表示宿主显式要求全量。
    mode: Literal["incremental", "full"] = "incremental"

    @field_validator("cursor", mode="before")
    @classmethod
    def empty_cursor(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PluginActorMoviePage(BaseModel):
    """演员影片列表的一页番号结果。"""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    movie_numbers: list[str] = Field(default_factory=list, max_length=500)
    next_cursor: str | None = Field(default=None, max_length=512)

    @field_validator("next_cursor", mode="before")
    @classmethod
    def empty_next_cursor(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("movie_numbers", mode="before")
    @classmethod
    def empty_movie_numbers(cls, value):
        return [] if value is None else value

    @field_validator("movie_numbers")
    @classmethod
    def clean_movie_numbers(cls, value):
        numbers: list[str] = []
        seen: set[str] = set()
        for number in value:
            normalized = (number or "").strip()
            if not normalized:
                continue
            if len(normalized) > 255:
                raise ValueError("movie_number 长度不能超过 255")
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            numbers.append(normalized)
        return numbers


class PluginMetadataSource(BaseModel):
    """按番号交付元数据，并可选择声明演员影片列表能力。

    宿主会调用所有声明该能力的启用来源，合并番号后按本地影片去重。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    fetch_movie: Callable[[str], PluginMovieMetadata | None]
    # None 表示插件不支持该能力；回调返回 None 表示无法识别演员。
    fetch_actor_movies: (
        Callable[[PluginActorMovieQuery], PluginActorMoviePage | None] | None
    ) = None


def validate_metadata_extension(
    *, plugin_id: str, extension: PluginExtension
) -> PluginMetadataSource:
    return PluginMetadataSource.model_validate(extension.data)
