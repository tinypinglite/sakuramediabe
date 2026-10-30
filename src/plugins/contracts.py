"""插件注册契约。

宿主接受受支持的 manifest 接口版本范围；旧插件运行时可保留 manifest 版本，
也可像现有官方插件一样动态声明当前宿主版本。

机制层只认识两类声明：后台任务（``jobs``，宿主平台能力）与
扩展点声明（``extensions``，业务领域扩展）。任何领域的扩展点载荷与
校验都由 ``src.plugins.extensions`` 下的领域模块负责，
核心契约不解释 ``PluginExtension.data``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.scheduler.contracts import JobDefinition

# 11：新增图片写入能力（context.movies.set_cover / set_plot_images、
# context.actors.set_profile_image / clear_profile_image）。
# 12：新增插件 i18n 能力（context.i18n.t、ranking/provider config 可选 i18n）。
# 13：新增插件种子文件提交能力（context.downloads.submit_torrent）。
HOST_API_VERSION = 13
MIN_SUPPORTED_HOST_API_VERSION = 4


def validate_host_api_version(version: int) -> None:
    if not MIN_SUPPORTED_HOST_API_VERSION <= version <= HOST_API_VERSION:
        raise ValueError(
            "Host API 版本不兼容: "
            f"plugin={version} "
            f"host=[{MIN_SUPPORTED_HOST_API_VERSION},{HOST_API_VERSION}]"
        )


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

    plugin_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1)
    # 可选多语言展示名；manifest 与注册声明都支持，缺失时回退 display_name。
    display_name_i18n: dict[str, str] = Field(default_factory=dict)
    version: str = Field(min_length=1)
    host_api_version: int = HOST_API_VERSION
    # 任务不要求至少一个：插件可以是纯能力型（未来扩展），零任务同样合法。
    jobs: tuple[JobDefinition, ...] = Field(default_factory=tuple)
    # 扩展点声明；默认空。核心不解释 data，领域校验由 extensions 目录登记。
    extensions: tuple[PluginExtension, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_host_api_version(self):
        validate_host_api_version(self.host_api_version)
        return self
