from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobDefinition(BaseModel):
    """统一的后台任务声明，供内建任务和插件任务共同使用。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_key: str = Field(min_length=1)
    log_name: str = Field(min_length=1)
    cli_name: str = Field(min_length=1)
    cli_help: str = Field(min_length=1)
    # 内建任务继续通过 Scheduler 的静态字段取 cron；插件任务使用 default_cron。
    cron_setting: str | None = None
    default_cron: str | None = None
    # cron 任务必须提供 service_factory；manual_only 任务可只提供 params_handler。
    service_factory: Callable[..., Any] | None = None
    # 业务回收仅处理每条记录的 running 状态，不再承担 task_run / mutex 回收。
    business_recovery: Callable[[], dict[str, int]] | None = None
    format_stats: Callable[[dict[str, Any]], str] | None = None
    # 是否允许通过 HTTP 接口手动触发；False 时仅保留 cron / CLI 两条路径。
    manual_trigger_allowed: bool = True
    # 手动带参任务：manual_only=True 时无 cron，只能通过 HTTP/CLI 带 params 触发。
    manual_only: bool = False
    # 带参执行体与参数模型：二者必须成对出现；定时触发仍走 service_factory。
    params_schema: type[BaseModel] | None = None
    params_handler: Callable[[Any, dict[str, Any]], Any] | None = None
    # 插件来源由 loader 注入；内建任务保持为 None。
    plugin_id: str | None = None

    @model_validator(mode="after")
    def _validate_cron_source(self):
        if self.service_factory is None and self.params_handler is None:
            raise ValueError("任务必须提供 service_factory 或 params_handler")
        if self.service_factory is None and not self.manual_only:
            raise ValueError("cron 任务必须提供 service_factory")
        if self.manual_only:
            if self.cron_setting is not None or self.default_cron is not None:
                raise ValueError(
                    "manual_only 任务不允许声明 cron（cron_setting/default_cron）"
                )
            if not self.manual_trigger_allowed:
                raise ValueError("manual_only 任务必须允许手动触发")
        elif self.cron_setting is None and self.default_cron is None:
            raise ValueError("任务必须声明 cron_setting/default_cron 或 manual_only")

        if (self.params_schema is None) != (self.params_handler is None):
            raise ValueError("params_schema 与 params_handler 必须成对声明")
        if (
            self.cron_setting is None
            and self.default_cron is None
            and not self.manual_only
        ):
            raise ValueError("任务必须声明 cron_setting 或 default_cron")
        if self.default_cron is not None:
            try:
                CronTrigger.from_crontab(self.default_cron)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"default_cron 不是合法的 cron 表达式: {self.default_cron}"
                ) from exc
        return self
