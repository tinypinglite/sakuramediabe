from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.scheduler.contracts import JobDefinition


HOST_API_VERSION = 1


class PluginRegistration(BaseModel):
    """插件注册入口返回的声明对象。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    plugin_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    host_api_version: int = HOST_API_VERSION
    jobs: tuple[JobDefinition, ...] = Field(min_length=1)
