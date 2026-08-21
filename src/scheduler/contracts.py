from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobExecutionError(ValueError):
    """任务声明无法按持久化参数组成执行体。"""


class JobDefinition(BaseModel):
    """统一的后台任务声明，供内建任务、插件任务和队列任务共同使用。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_key: str = Field(min_length=1)
    log_name: str = Field(min_length=1)
    cli_name: str = Field(min_length=1)
    cli_help: str = Field(min_length=1)
    # 内建任务继续通过 Scheduler 的静态字段取 cron；插件任务使用 default_cron。
    cron_setting: str | None = None
    default_cron: str | None = None
    # 任务终态后联动清理领域状态。
    business_recovery: Callable[[], dict[str, int]] | None = None
    # 是否允许通过 HTTP 接口手动触发；False 时仅保留 cron / CLI 两条路径。
    manual_trigger_allowed: bool = True
    # 手动带参任务：manual_only=True 时无 cron，只能通过 HTTP/CLI 带 params 触发。
    manual_only: bool = False
    params_schema: type[BaseModel] | None = None
    # 所有任务统一使用这个入口；参数为空时 worker 传入空对象。
    handler: Callable[[Any, dict[str, Any]], Any]
    # 队列领取道和任务级通知策略也属于任务声明，避免再维护私有覆盖表。
    lane: str = "default"
    notify_result: bool = True
    # 插件来源由 loader 注入；内建任务保持为 None。
    plugin_id: str | None = None

    @model_validator(mode="after")
    def _validate_cron_source(self):
        if self.manual_only:
            if self.cron_setting is not None or self.default_cron is not None:
                raise ValueError(
                    "manual_only 任务不允许声明 cron（cron_setting/default_cron）"
                )
            if not self.manual_trigger_allowed:
                raise ValueError("manual_only 任务必须允许手动触发")
        elif self.cron_setting is None and self.default_cron is None:
            raise ValueError("任务必须声明 cron_setting/default_cron 或 manual_only")

        if self.default_cron is not None:
            try:
                CronTrigger.from_crontab(self.default_cron)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"default_cron 不是合法的 cron 表达式: {self.default_cron}"
                ) from exc
        return self

    def build_executor(self, raw_params: dict[str, Any] | None):
        """把持久化参数绑定到统一任务处理器。"""
        if raw_params is not None and not isinstance(raw_params, dict):
            raise JobExecutionError(
                f"任务参数必须是 JSON object task_key={self.task_key}"
            )
        params = raw_params or {}
        return lambda reporter: self.handler(reporter, params)
