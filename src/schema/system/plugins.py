"""插件管理接口的响应模型。"""

from typing import Any

from pydantic import Field

from src.schema.common.base import SchemaModel


class PluginSummaryResource(SchemaModel):
    plugin_id: str
    display_name: str
    version: str
    host_api_version: int
    enabled: bool
    load_status: str = "ok"
    load_error: str | None = None
    release_api_url: str | None = None


class PluginDetailResource(PluginSummaryResource):
    requires_python: str | None = None
    author: str | None = None
    homepage: str | None = None
    manifest: dict
    data_dir: str


class PluginInstallResponse(SchemaModel):
    plugin_id: str
    version: str
    pending_restart: list[str]


class PluginSettingsResource(SchemaModel):
    """插件私有配置（`plugins.settings.<plugin_id>`）的明文快照。"""

    settings: dict[str, Any]
    settings_schema: dict[str, Any] | None = Field(default=None, alias="schema")
    defaults: dict[str, Any] | None = None


class PluginSettingsUpdateResource(PluginSettingsResource):
    """插件配置写入结果；配置在 api/aps import 期读取，必须重启两个进程。"""

    pending_restart: list[str]


class MetadataSourceResource(SchemaModel):
    plugin_id: str
    display_name: str


class MetadataSourceOrderRequest(SchemaModel):
    plugin_ids: list[str]


class MetadataSourceOrderResource(SchemaModel):
    items: list[MetadataSourceResource]
    pending_restart: list[str]


class StatsFieldResource(SchemaModel):
    key: str
    label: str
    value_type: str
    min: float = 0
    max: float | None = None
    unit: str | None = None


class PluginBindingSourceResource(SchemaModel):
    plugin_id: str
    display_name: str


class PluginBindingResource(SchemaModel):
    stats_source: str | None = None
    review_source: str | None = None
    stats_source_available: bool = False
    review_source_available: bool = False
    stats_source_display_name: str | None = None
    review_source_display_name: str | None = None
    stats_fields: list[StatsFieldResource] = Field(default_factory=list)
    heat_version: str | None = None
    available_stats_sources: list[PluginBindingSourceResource] = Field(
        default_factory=list
    )
    available_review_sources: list[PluginBindingSourceResource] = Field(
        default_factory=list
    )


class PluginBindingUpdateRequest(SchemaModel):
    stats_source: str | None = None
    review_source: str | None = None


class PluginBindingUpdateResource(PluginBindingResource):
    pending_restart: list[str]
