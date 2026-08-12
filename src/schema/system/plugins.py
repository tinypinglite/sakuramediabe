"""插件管理接口的响应模型。"""

from src.schema.common.base import SchemaModel


class PluginSummaryResource(SchemaModel):
    plugin_id: str
    display_name: str
    version: str
    host_api_version: int
    enabled: bool
    load_status: str = "ok"
    load_error: str | None = None


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
