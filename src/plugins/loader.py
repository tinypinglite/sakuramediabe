from __future__ import annotations

import re
from copy import deepcopy
from importlib import import_module
from types import MappingProxyType
from typing import Callable

from apscheduler.triggers.cron import CronTrigger

from src.config.config import Plugins
from src.plugins.contracts import HOST_API_VERSION, PluginRegistration
from src.plugins.context import PluginContext
from src.scheduler.contracts import JobDefinition

PLUGIN_MODULE_PREFIX = "src.plugins.extensions"
PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class PluginLoadError(RuntimeError):
    """已显式启用的插件无法完成加载。"""

    def __init__(self, plugin_id: str, stage: str, message: str):
        self.plugin_id = plugin_id
        self.stage = stage
        super().__init__(f"插件加载失败 plugin_id={plugin_id} stage={stage}: {message}")


def _validate_plugin_jobs(
    *,
    plugin_id: str,
    registration: PluginRegistration,
    configured_crons: dict[str, str],
) -> tuple[JobDefinition, ...]:
    task_keys = {job.task_key for job in registration.jobs}
    if len(task_keys) != len(registration.jobs):
        raise PluginLoadError(plugin_id, "validate_jobs", "插件内部 task_key 重复")

    unknown_cron_keys = sorted(set(configured_crons) - task_keys)
    if unknown_cron_keys:
        raise PluginLoadError(
            plugin_id,
            "validate_cron",
            f"cron 覆盖引用了插件未注册的任务: {', '.join(unknown_cron_keys)}",
        )

    for task_key, cron_expr in configured_crons.items():
        try:
            CronTrigger.from_crontab(cron_expr)
        except (TypeError, ValueError) as exc:
            raise PluginLoadError(
                plugin_id,
                "validate_cron",
                f"任务 {task_key} 的 cron 表达式非法: {cron_expr}",
            ) from exc

    jobs: list[JobDefinition] = []
    for job in registration.jobs:
        if job.plugin_id is not None:
            raise PluginLoadError(
                plugin_id,
                "validate_jobs",
                f"任务 {job.task_key} 不允许自行声明 plugin_id",
            )
        if job.cron_setting is not None:
            raise PluginLoadError(
                plugin_id,
                "validate_jobs",
                f"任务 {job.task_key} 不允许引用宿主 Scheduler 字段",
            )
        if job.default_cron is None:
            raise PluginLoadError(
                plugin_id,
                "validate_jobs",
                f"任务 {job.task_key} 必须声明 default_cron",
            )
        jobs.append(job.model_copy(update={"plugin_id": plugin_id}))
    return tuple(jobs)


def load_enabled_plugins(
    plugin_settings: Plugins,
    *,
    module_importer: Callable[[str], object] = import_module,
) -> tuple[PluginRegistration, ...]:
    """严格按 enabled 顺序加载插件；未启用插件不会被 import。"""
    loaded: list[PluginRegistration] = []
    for plugin_id in plugin_settings.enabled:
        if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
            raise PluginLoadError(plugin_id, "validate_id", "插件 ID 格式非法")

        module_name = f"{PLUGIN_MODULE_PREFIX}.{plugin_id}"
        try:
            module = module_importer(module_name)
        except Exception as exc:
            raise PluginLoadError(
                plugin_id,
                "import",
                f"无法导入模块 {module_name}",
            ) from exc

        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginLoadError(
                plugin_id,
                "resolve_register",
                f"模块 {module_name} 未暴露可调用的 register(context)",
            )

        raw_settings = deepcopy(plugin_settings.settings.get(plugin_id, {}))
        context = PluginContext(
            plugin_id=plugin_id,
            settings=MappingProxyType(raw_settings),
        )
        try:
            registration = PluginRegistration.model_validate(register(context))
        except Exception as exc:
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(
                plugin_id,
                "register",
                "register(context) 执行或返回值校验失败",
            ) from exc

        if registration.plugin_id != plugin_id:
            raise PluginLoadError(
                plugin_id,
                "validate_registration",
                f"register 返回的 plugin_id={registration.plugin_id} 与配置不一致",
            )
        if registration.host_api_version != HOST_API_VERSION:
            raise PluginLoadError(
                plugin_id,
                "validate_registration",
                "Host API 版本不兼容: "
                f"plugin={registration.host_api_version} host={HOST_API_VERSION}",
            )

        jobs = _validate_plugin_jobs(
            plugin_id=plugin_id,
            registration=registration,
            configured_crons=plugin_settings.job_crons.get(plugin_id, {}),
        )
        loaded.append(registration.model_copy(update={"jobs": jobs}))
    return tuple(loaded)
