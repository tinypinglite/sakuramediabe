"""排行榜扩展点（``discovery.ranking_source``）。

这是排行榜领域契约：插件用
``PluginExtension(key=RANKING_SOURCE_EXTENSION_KEY, data=PluginRankingSource(...))``
声明来源。领域校验器把排行榜约束从通用机制中隔离出来，
由 loader/adapter 按 key 委托调用；本模块不依赖 service/scheduler，
避免加载链反向依赖任务注册表。
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.plugins.contracts import PluginExtension

# 排行榜来源扩展点 key；插件从 src.plugins 顶层导入。
RANKING_SOURCE_EXTENSION_KEY = "discovery.ranking_source"


class PluginRankingBoard(BaseModel):
    """单个排行榜榜单声明；抓取逻辑由插件实现，宿主只负责编排与写库。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    # 静态周期；空元组表示单期榜（API 不接受 period）。与 supported_periods_provider 二选一。
    supported_periods: tuple[str, ...] = Field(default_factory=tuple)
    default_period: str | None = None
    # 需要宿主 metadata 账号才抓；未配置账号时宿主跳过该 board，凭据不进插件。
    requires_account: bool = False
    # 抓取前回调：(period, 该 board+period 是否已有数据) -> 是否抓。
    should_fetch: Callable[[str, bool], bool] | None = None
    # 动态周期提供者（如 top250 年份逐年滚动）；要求纯本地计算、幂等、无副作用。
    supported_periods_provider: Callable[[], tuple[str, ...]] | None = None
    # 返回番号列表；列表顺序即 rank；异常向上冒泡给宿主统一处理。
    fetch_numbers: Callable[[str], list[str]]

    @model_validator(mode="after")
    def _validate_periods(self):
        if self.supported_periods and self.supported_periods_provider is not None:
            raise ValueError(
                "supported_periods 与 supported_periods_provider 只能二选一"
            )
        if (
            not self.supported_periods
            and self.supported_periods_provider is None
            and self.default_period is not None
        ):
            raise ValueError("单期榜（无任何周期）不允许声明 default_period")
        for period in self.supported_periods:
            if len(period) > 32:
                raise ValueError(f"period 长度超过 32: {period}")
        if (
            self.supported_periods
            and self.default_period is not None
            and self.default_period not in self.supported_periods
        ):
            raise ValueError(
                f"default_period 不在 supported_periods 中: {self.default_period}"
            )
        return self


class PluginRankingSource(BaseModel):
    """排行榜来源声明；source_key 全局唯一、不加前缀、不设保留字。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    source_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    boards: tuple[PluginRankingBoard, ...] = Field(min_length=1)


def validate_ranking_extension(
    *,
    plugin_id: str,
    extension: PluginExtension,
) -> PluginRankingSource:
    """校验并返回排行榜扩展点载荷。

    只做排行榜领域约束（载荷类型、榜单 key 唯一、动态周期结果、
    default_period 归属）；失败抛 ValueError，由 loader/adapter
    统一包成 PluginLoadError 并隔离插件。
    """
    if extension.key != RANKING_SOURCE_EXTENSION_KEY:
        raise ValueError(f"扩展点 key 不匹配: {extension.key}")
    if isinstance(extension.data, PluginRankingSource):
        source = extension.data
    else:
        try:
            source = PluginRankingSource.model_validate(extension.data)
        except Exception as exc:
            raise ValueError(
                f"排行榜扩展点 data 不是合法 PluginRankingSource: {exc}"
            ) from exc

    board_keys = {board.key for board in source.boards}
    if len(board_keys) != len(source.boards):
        raise ValueError(f"来源 {source.source_key} 内部 board.key 重复")
    for board in source.boards:
        try:
            periods = (
                board.supported_periods_provider()
                if board.supported_periods_provider is not None
                else board.supported_periods
            )
        except Exception as exc:
            raise ValueError(
                f"动态周期计算失败 source={source.source_key} board={board.key}: {exc}"
            ) from exc
        periods = tuple(periods)
        for period in periods:
            if len(period) > 32:
                raise ValueError(
                    f"period 长度超过 32: "
                    f"source={source.source_key} board={board.key} period={period}"
                )
        if board.default_period is not None and board.default_period not in periods:
            raise ValueError(
                f"default_period 不在最终周期集合中: "
                f"source={source.source_key} board={board.key} "
                f"period={board.default_period}"
            )
    return source
