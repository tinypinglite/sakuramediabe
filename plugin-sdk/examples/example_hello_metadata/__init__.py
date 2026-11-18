"""示例元数据源插件。"""

from .plugin import register
from .settings import Settings

__all__ = ["Settings", "register"]
