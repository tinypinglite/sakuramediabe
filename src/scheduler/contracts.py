from __future__ import annotations

from typing import Any, Callable

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
    service_factory: Callable[..., Any]
    # 业务回收仅处理每条记录的 running 状态，不再承担 task_run / mutex 回收。
    business_recovery: Callable[[], dict[str, int]] | None = None
    format_stats: Callable[[dict[str, Any]], str] | None = None
    # 是否允许通过 HTTP 接口手动触发；False 时仅保留 cron / CLI 两条路径。
    manual_trigger_allowed: bool = True
    # 插件来源由 loader 注入；内建任务保持为 None。
    plugin_id: str | None = None

    @model_validator(mode="after")
    def _validate_cron_source(self):
        if self.cron_setting is None and self.default_cron is None:
            raise ValueError("任务必须声明 cron_setting 或 default_cron")
        if self.default_cron is not None:
            try:
                CronTrigger.from_crontab(self.default_cron)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"default_cron 不是合法的 cron 表达式: {self.default_cron}"
                ) from exc
        return self
