"""按番号提供互动数快照；热度由插件计算，宿主只校验和持久化。"""

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.plugins.contracts import PluginExtension

STATS_SOURCE_EXTENSION_KEY = "catalog.movie_stats"
STATS_SOURCE_HOST_API_VERSION = 18


class PluginMovieStatsField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    value_type: Literal["int", "float"] = "int"
    label_i18n: dict[str, str] = Field(default_factory=dict)
    min: float = Field(default=0, ge=0)
    max: float | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_range(self):
        if self.max is not None and self.max < self.min:
            raise ValueError("stats field max 不能小于 min")
        if self.key == "heat":
            raise ValueError("heat 是保留字段，不能出现在互动数 values 中")
        return self


class PluginMovieStatsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    values: dict[str, int | float] = Field(default_factory=dict)
    heat: int = Field(ge=0, strict=True)


class PluginMovieStatsSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    fields: list[PluginMovieStatsField] = Field(default_factory=list)
    heat_version: str = Field(min_length=1, max_length=128)
    fetch_stats: Callable[[str], PluginMovieStatsSnapshot | dict | None]

    @model_validator(mode="after")
    def validate_fields(self):
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("互动数字段 key 不能重复")
        return self


def validate_stats_extension(
    *, plugin_id: str, extension: PluginExtension
) -> PluginMovieStatsSource:
    return PluginMovieStatsSource.model_validate(extension.data)


def validate_stats_snapshot(
    source: PluginMovieStatsSource,
    snapshot: PluginMovieStatsSnapshot,
) -> PluginMovieStatsSnapshot:
    fields = {field.key: field for field in source.fields}
    unknown = sorted(set(snapshot.values) - set(fields))
    if unknown:
        raise ValueError(f"插件返回了未声明的互动数字段: {', '.join(unknown)}")
    for key, value in snapshot.values.items():
        field = fields[key]
        if isinstance(value, bool):
            raise TypeError(f"互动数字段 {key} 不能是布尔值")
        if field.value_type == "int" and type(value) is not int:
            raise ValueError(f"互动数字段 {key} 必须是整数")
        if field.value_type == "float" and type(value) not in {int, float}:
            raise ValueError(f"互动数字段 {key} 必须是数字")
        numeric = float(value)
        if numeric < field.min:
            raise ValueError(f"互动数字段 {key} 不能小于 {field.min}")
        if field.max is not None and numeric > field.max:
            raise ValueError(f"互动数字段 {key} 不能大于 {field.max}")
    return snapshot


__all__ = [
    "STATS_SOURCE_EXTENSION_KEY",
    "STATS_SOURCE_HOST_API_VERSION",
    "PluginMovieStatsField",
    "PluginMovieStatsSnapshot",
    "PluginMovieStatsSource",
    "validate_stats_extension",
    "validate_stats_snapshot",
]
