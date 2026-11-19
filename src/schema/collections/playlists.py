from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from src.i18n.playlist_names import (
    localize_system_playlist_description,
    localize_system_playlist_name,
)
from src.model import (
    PLAYLIST_KIND_CUSTOM,
    PLAYLIST_KIND_SMART,
    SYSTEM_PLAYLIST_KINDS,
)
from src.schema.catalog.movies import (
    MovieCollectionType,
    MovieListItemResource,
    MovieListStatus,
    MovieNumberSource,
    MovieWatchStatus,
    TagMatchMode,
)
from src.schema.common.base import SchemaModel
from src.schema.common.batch import MAX_BATCH_ITEMS


class SmartPlaylistFilterResource(SchemaModel):
    """智能列表保存的筛选条件；字段与影片列表接口的查询参数一一对应。

    多选维度：状态、观看状态、年份、分辨率、标签、媒体标签、女优。
    空列表表示「不限」；年份/分辨率只按「任一」匹配，其余多选维度可切换
    「任一 / 全部」。
    """

    statuses: list[MovieListStatus] = Field(default_factory=list)
    status_match: TagMatchMode = TagMatchMode.OR
    watch_statuses: list[MovieWatchStatus] = Field(default_factory=list)
    watch_match: TagMatchMode = TagMatchMode.OR
    collection_type: MovieCollectionType = MovieCollectionType.ALL
    number_source: MovieNumberSource = MovieNumberSource.ALL
    sort: str | None = None
    years: list[int] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    tag_match: TagMatchMode = TagMatchMode.OR
    media_tag_ids: list[int] = Field(default_factory=list)
    media_tag_match: TagMatchMode = TagMatchMode.OR
    actor_age_min: int | None = None
    actor_age_max: int | None = None
    heat_min: int | None = None
    heat_max: int | None = None
    actor_ids: list[int] = Field(default_factory=list)
    actor_match: TagMatchMode = TagMatchMode.OR

    @model_validator(mode="before")
    @classmethod
    def lift_legacy_single_filters(cls, data):
        """兼容早期单值快照（status/watch_status/resolution）。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy_status = data.pop("status", None)
        if (
            legacy_status
            and legacy_status != MovieListStatus.ALL.value
            and not data.get("statuses")
        ):
            data["statuses"] = [legacy_status]
        legacy_watch_status = data.pop("watch_status", None)
        if (
            legacy_watch_status
            and legacy_watch_status != MovieWatchStatus.ALL.value
            and not data.get("watch_statuses")
        ):
            data["watch_statuses"] = [legacy_watch_status]
        legacy_resolution = data.pop("resolution", None)
        if legacy_resolution and not data.get("resolutions"):
            data["resolutions"] = [legacy_resolution]
        return data

    @field_validator("years")
    @classmethod
    def validate_years(cls, value: list[int]) -> list[int]:
        # 与影片列表一致：年份参与 year+1 的区间构造，超出 datetime 范围直接拒绝。
        if any(year < 1 or year > 9998 for year in value):
            raise ValueError("year 超出可用范围")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "SmartPlaylistFilterResource":
        ages = [
            age
            for age in (self.actor_age_min, self.actor_age_max)
            if age is not None
        ]
        if any(age < 0 or age > 200 for age in ages):
            raise ValueError("actor_age 超出可用范围")
        if (
            self.actor_age_min is not None
            and self.actor_age_max is not None
            and self.actor_age_min > self.actor_age_max
        ):
            raise ValueError("actor_age_min 不能大于 actor_age_max")
        if (
            self.heat_min is not None
            and self.heat_max is not None
            and self.heat_min > self.heat_max
        ):
            raise ValueError("heat_min 不能大于 heat_max")
        return self


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
    is_smart: bool = False
    smart_filter: SmartPlaylistFilterResource | None = None
    movie_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_playlist(cls, playlist, movie_count: int = 0) -> "PlaylistResource":
        is_system = playlist.kind in cls.SYSTEM_KINDS
        is_smart = playlist.kind == PLAYLIST_KIND_SMART
        extra: dict = {
            "is_system": is_system,
            "is_mutable": not is_system,
            "is_deletable": not is_system,
            "is_smart": is_smart,
            "movie_count": movie_count,
        }
        if is_smart:
            extra["smart_filter"] = SmartPlaylistFilterResource.model_validate(
                playlist.filter_json or {}
            )
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
    kind: Literal["custom", "smart"] = PLAYLIST_KIND_CUSTOM
    filter: SmartPlaylistFilterResource | None = None

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
    filter: SmartPlaylistFilterResource | None = None

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
