"""SakuraMedia 仓库内可信插件基础设施。"""

from .contracts import HOST_API_VERSION, PluginRegistration
from .context import PluginContext

__all__ = ["HOST_API_VERSION", "PluginContext", "PluginRegistration"]
