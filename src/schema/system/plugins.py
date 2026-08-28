"""插件管理接口的响应模型。"""

from typing import Any

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


class PluginSettingsUpdateResource(PluginSettingsResource):
    """插件配置写入结果；配置在 api/aps import 期读取，必须重启两个进程。"""

    pending_restart: list[str]
