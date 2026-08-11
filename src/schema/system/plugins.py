from src.schema.common.base import SchemaModel


class PluginSummaryResource(SchemaModel):
    plugin_id: str
    display_name: str
    version: str
    host_api_version: int
    enabled: bool
    deps_status: str
    load_status: str = "ok"
    load_error: str | None = None
    installed_at: str | None = None


class PluginDetailResource(PluginSummaryResource):
    requires_python: str | None = None
    author: str | None = None
    homepage: str | None = None
    dependencies: dict
    manifest: dict
    dists: dict[str, str]
    data_dir: str
    install_log_tail: str


class PluginInstallResponse(SchemaModel):
    plugin_id: str
    version: str
    pending_restart: list[str]
