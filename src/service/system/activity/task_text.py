"""任务名/任务说明的写时翻译。

任务名在创建 BackgroundTaskRun 时落库，之后不再重译；插件任务用
``cli_help_i18n``，宿主任务用 ``tasks.<task_key>.name``，缺失时保留原有中文。
"""

from __future__ import annotations

from src.i18n import translate
from src.plugins.i18n import resolve_text
from src.scheduler.contracts import JobDefinition
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY


def resolve_task_name(task_key: str, task_name: str | None = None) -> str:
    """解析宿主任务名；动态传入的名称（不等于注册表名）保持原样。"""
    registry_name = TASK_NAME_REGISTRY.get(task_key)
    provided = (task_name or "").strip()
    if provided and provided != registry_name:
        return provided
    translated = translate(f"tasks.{task_key}.name", default="")
    return translated or provided or registry_name or task_key


def resolve_job_cli_help(job_def: JobDefinition) -> str:
    return resolve_text(job_def.cli_help, job_def.cli_help_i18n)


def resolve_job_task_name(job_def: JobDefinition) -> str:
    if job_def.plugin_id is not None:
        return resolve_job_cli_help(job_def)
    translated = translate(f"tasks.{job_def.task_key}.name", default="")
    return translated or TASK_NAME_REGISTRY.get(job_def.task_key) or job_def.cli_help
