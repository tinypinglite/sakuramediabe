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
    # 宿主内建的无参/混合任务仍可使用这两个内部执行入口；插件只能使用 handler。
    service_factory: Callable[..., Any] | None = None
    # 业务回收仅处理每条记录的 running 状态，不再承担 task_run / mutex 回收。
    business_recovery: Callable[[], dict[str, int]] | None = None
    # 是否允许通过 HTTP 接口手动触发；False 时仅保留 cron / CLI 两条路径。
    manual_trigger_allowed: bool = True
    # 手动带参任务：manual_only=True 时无 cron，只能通过 HTTP/CLI 带 params 触发。
    manual_only: bool = False
    # 宿主旧内建任务的参数入口；新插件统一使用 handler + params_schema。
    params_schema: type[BaseModel] | None = None
    params_handler: Callable[[Any, dict[str, Any]], Any] | None = None
    # 队列任务统一使用这个入口；参数为空时 worker 传入空对象。
    handler: Callable[[Any, dict[str, Any]], Any] | None = None
    # 队列领取道和任务级通知策略也属于任务声明，避免再维护私有覆盖表。
    lane: str = "default"
    notify_result: bool = True
    # 插件来源由 loader 注入；内建任务保持为 None。
    plugin_id: str | None = None

    @model_validator(mode="after")
    def _validate_cron_source(self):
        if (
            self.service_factory is None
            and self.params_handler is None
            and self.handler is None
        ):
            raise ValueError("任务必须提供 service_factory、params_handler 或 handler")
        if self.service_factory is None and self.handler is None and not self.manual_only:
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

        if self.params_handler is not None and self.params_schema is None:
            raise ValueError("params_schema 与 params_handler 必须成对声明")
        if (
            self.params_schema is not None
            and self.params_handler is None
            and self.handler is None
        ):
            raise ValueError("params_schema 必须配合 params_handler 或 handler")
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

    def build_executor(self, raw_params: dict[str, Any] | None):
        """根据队列里保存的 NULL / JSON object 选择唯一执行入口。"""
        if raw_params is not None and not isinstance(raw_params, dict):
            raise JobExecutionError(
                f"任务参数必须是 JSON object task_key={self.task_key}"
            )

        if self.handler is not None:
            params = raw_params or {}

            def run_handler(reporter):
                return self.handler(reporter, params)

            return run_handler

        if raw_params is not None:
            if self.params_handler is None:
                raise JobExecutionError(f"任务不支持带参执行 task_key={self.task_key}")
            params = raw_params

            def run_params_handler(reporter):
                return self.params_handler(reporter, params)

            return run_params_handler

        if self.service_factory is not None:
            return self.service_factory
        raise JobExecutionError(f"任务缺少无参执行体 task_key={self.task_key}")
