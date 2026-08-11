"""插件注册契约（v2，完全重写，不兼容旧版）。

宿主声明当前接口版本与最低兼容版本；插件声明自己面向的版本，
加载时要求 ``MIN_SUPPORTED <= plugin <= HOST_API_VERSION``。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.scheduler.contracts import JobDefinition

HOST_API_VERSION = 1
MIN_SUPPORTED_HOST_API_VERSION = 1


class PluginRegistration(BaseModel):
    """插件 register(context) 返回的声明对象。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plugin_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    host_api_version: int = HOST_API_VERSION
    # 任务不要求至少一个：插件可以是纯能力型（未来扩展），零任务同样合法。
    jobs: tuple[JobDefinition, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_host_api_version(self):
        if not (
            MIN_SUPPORTED_HOST_API_VERSION
            <= self.host_api_version
            <= HOST_API_VERSION
        ):
            raise ValueError(
                "Host API 版本不兼容: "
                f"plugin={self.host_api_version} "
                f"host=[{MIN_SUPPORTED_HOST_API_VERSION},{HOST_API_VERSION}]"
            )
        return self
