"""插件注册契约。

宿主只接受当前接口版本；旧版本插件在加载阶段直接拒绝。

机制层只认识两类声明：后台任务（``jobs``，宿主平台能力）与
扩展点声明（``extensions``，业务领域扩展）。任何领域的扩展点载荷与
校验都由 ``src.plugins.extensions`` 下的领域模块负责，
核心契约不解释 ``PluginExtension.data``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.scheduler.contracts import JobDefinition

HOST_API_VERSION = 3
MIN_SUPPORTED_HOST_API_VERSION = HOST_API_VERSION


class PluginExtension(BaseModel):
    """通用扩展点声明；核心机制只做结构校验，不解释 data 的领域语义。

    key 使用点分命名空间（如 ``discovery.ranking_source``），由宿主扩展点
    目录登记；data 是领域载荷（可以是带回调的模型实例），由对应领域的
    校验器在加载/装配时解释。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$",
    )
    data: Any

    @model_validator(mode="after")
    def _require_data(self):
        if self.data is None:
            raise ValueError("扩展点 data 不能为空")
        return self


class PluginRegistration(BaseModel):
    """插件 register(context) 返回的声明对象。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    plugin_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    host_api_version: int = HOST_API_VERSION
    # 任务不要求至少一个：插件可以是纯能力型（未来扩展），零任务同样合法。
    jobs: tuple[JobDefinition, ...] = Field(default_factory=tuple)
    # 扩展点声明；默认空。核心不解释 data，领域校验由 extensions 目录登记。
    extensions: tuple[PluginExtension, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_host_api_version(self):
        if self.host_api_version != HOST_API_VERSION:
            raise ValueError(
                "Host API 版本不兼容: "
                f"plugin={self.host_api_version} "
                f"host=[{MIN_SUPPORTED_HOST_API_VERSION},{HOST_API_VERSION}]"
            )
        return self
