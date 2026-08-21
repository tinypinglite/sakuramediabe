"""插件加载器。

从插件根目录加载显式启用的插件：import 包根 register() → 契约校验 →
注入 plugin_id 并返回注册声明。单个插件失败记入 PLUGIN_LOAD_ERRORS
并跳过，服务与 CLI 照常启动。
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.config.config import Plugins
from src.plugins.context import PluginContext
from src.plugins.contracts import (
    HOST_API_VERSION,
    MIN_SUPPORTED_HOST_API_VERSION,
    PluginRegistration,
)
from src.plugins.extensions import EXTENSION_VALIDATORS
from src.plugins.manifest import MANIFEST_FILENAME, load_manifest_from_file
from src.scheduler.contracts import JobDefinition

PLUGIN_MODULE_NAMESPACE = "sakuramedia_plugins"

# 插件加载错误登记：坏插件隔离，不拖垮整个服务；CLI 据此展示。
PLUGIN_LOAD_ERRORS: dict[str, dict[str, str]] = {}


class PluginLoadError(RuntimeError):
    """已显式启用的插件无法完成加载。"""

    def __init__(self, plugin_id: str, stage: str, message: str):
        self.plugin_id = plugin_id
        self.stage = stage
        super().__init__(f"插件加载失败 plugin_id={plugin_id} stage={stage}: {message}")


def _clear_plugin_modules(plugin_id: str) -> None:
    """清除插件根模块及其子模块，避免更新时混用旧代码。"""
    module_name = f"{PLUGIN_MODULE_NAMESPACE}.{plugin_id}"
    for name in list(sys.modules):
        if name == module_name or name.startswith(f"{module_name}."):
            sys.modules.pop(name, None)


def _import_plugin_package(plugin_dir: Path, plugin_id: str):
    """把插件目录作为包导入（支持插件内部相对导入）。"""
    init_path = plugin_dir / "__init__.py"
    module_name = f"{PLUGIN_MODULE_NAMESPACE}.{plugin_id}"
    # 清除同进程内可能残留的旧模块（例如加载失败后重试），避免复用过期状态。
    _clear_plugin_modules(plugin_id)
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_path,
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(plugin_id, "import", "无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    # 先注册再 exec，保证插件内部的相对导入（from .settings import ...）可解析。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _freeze_settings(value: Any) -> Any:
    """把配置递归冻结为只读结构：dict -> MappingProxyType，list -> tuple。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_settings(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_settings(item) for item in value)
    return value


def _validate_plugin_jobs(
    *,
    plugin_id: str,
    registration: PluginRegistration,
) -> tuple[JobDefinition, ...]:
    """插件任务校验：task_key 唯一、禁止自填来源、禁止引用宿主 Scheduler 字段。"""
    task_keys = {job.task_key for job in registration.jobs}
    if len(task_keys) != len(registration.jobs):
        raise PluginLoadError(plugin_id, "validate_jobs", "插件内部 task_key 重复")

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
        jobs.append(job.model_copy(update={"plugin_id": plugin_id}))
    return tuple(jobs)


def _validate_plugin_extensions(
    *,
    plugin_id: str,
    registration: PluginRegistration,
) -> None:
    """扩展点声明通用校验：key 唯一、key 受宿主支持，并委托领域校验器。

    这里不感知任何领域语义；data 的形状与约束由按 key 注册的校验器解释，
    失败统一走 PluginLoadError，保持坏插件隔离。
    """
    seen: set[str] = set()
    for extension in registration.extensions:
        if extension.key in seen:
            raise PluginLoadError(
                plugin_id,
                "validate_extensions",
                f"扩展点 key 重复: {extension.key}",
            )
        seen.add(extension.key)
        validator = EXTENSION_VALIDATORS.get(extension.key)
        if validator is None:
            raise PluginLoadError(
                plugin_id,
                "validate_extensions",
                f"宿主不支持该扩展点: {extension.key}",
            )
        try:
            validator(plugin_id=plugin_id, extension=extension)
        except Exception as exc:
            raise PluginLoadError(
                plugin_id,
                "validate_extensions",
                f"扩展点 {extension.key} 校验失败: {exc}",
            ) from exc


def _load_plugin_dir(
    *,
    plugin_id: str,
    plugin_dir: Path,
    plugin_settings: Plugins,
) -> PluginRegistration:
    """加载单个插件目录：manifest → import → register → 契约校验。"""
    manifest = load_manifest_from_file(plugin_dir)
    if manifest.plugin_id != plugin_id:
        raise PluginLoadError(
            plugin_id, "resolve", f"manifest.plugin_id={manifest.plugin_id} 与启用项不一致"
        )

    try:
        module = _import_plugin_package(plugin_dir, plugin_id)
    except Exception as exc:
        raise PluginLoadError(plugin_id, "import", f"无法导入插件包: {exc}") from exc

    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginLoadError(
            plugin_id, "resolve_register", "插件包根未暴露可调用的 register(context)"
        )

    context = PluginContext(
        plugin_id=plugin_id,
        settings=_freeze_settings(
            deepcopy(plugin_settings.settings.get(plugin_id, {}))
        ),
        data_dir=plugin_dir / "data",
    )
    context.ensure_data_dir()
    try:
        registration = PluginRegistration.model_validate(register(context))
    except Exception as exc:
        if isinstance(exc, PluginLoadError):
            raise
        raise PluginLoadError(
            plugin_id,
            "register",
            f"register(context) 执行或返回值校验失败: {exc}",
        ) from exc

    if registration.plugin_id != plugin_id:
        raise PluginLoadError(
            plugin_id,
            "validate_registration",
            f"register 返回的 plugin_id={registration.plugin_id} 与配置不一致",
        )
    if registration.version != manifest.version:
        raise PluginLoadError(
            plugin_id,
            "validate_registration",
            "register 返回的 version 与 manifest 不一致: "
            f"register={registration.version} manifest={manifest.version}",
        )
    if manifest.host_api_version != HOST_API_VERSION:
        raise PluginLoadError(
            plugin_id,
            "validate_registration",
            "manifest 声明的 Host API 版本不兼容: "
            f"manifest={manifest.host_api_version} "
            f"host=[{MIN_SUPPORTED_HOST_API_VERSION},{HOST_API_VERSION}]",
        )
    if registration.host_api_version != manifest.host_api_version:
        raise PluginLoadError(
            plugin_id,
            "validate_registration",
            "register 返回的 host_api_version 与 manifest 不一致",
        )

    jobs = _validate_plugin_jobs(
        plugin_id=plugin_id,
        registration=registration,
    )
    _validate_plugin_extensions(
        plugin_id=plugin_id,
        registration=registration,
    )
    return registration.model_copy(update={"jobs": jobs})


def check_plugin_dir(
    *,
    plugin_dir: Path,
    plugin_settings: Plugins | None = None,
) -> PluginRegistration:
    """校验一个插件目录（import + register + 契约），供 ``plugins check`` 使用。

    插件 ID 取目录名；失败抛 PluginLoadError。校验后清理该插件命名空间的
    sys.modules 副作用，避免污染后续加载。
    """
    plugin_dir = Path(plugin_dir)
    plugin_id = plugin_dir.name
    try:
        return _load_plugin_dir(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            plugin_settings=plugin_settings or Plugins(),
        )
    finally:
        _clear_plugin_modules(plugin_id)


def load_enabled_plugins(
    plugin_settings: Plugins,
    *,
    root_dir: Path,
) -> tuple[PluginRegistration, ...]:
    """按 enabled 顺序加载插件；单个插件失败记入 PLUGIN_LOAD_ERRORS 并跳过。

    未启用插件不会被导入。坏插件隔离而不是整体 fail-fast：第三方插件出错时
    服务与 CLI 仍可用，CLI 能看到错误原因并允许禁用/删除。
    """
    root = Path(root_dir)
    loaded: list[PluginRegistration] = []
    # 只保留当前启用集合的加载错误，避免停用/修复后残留过期状态。
    enabled_ids = set(plugin_settings.enabled)
    for stale_id in list(PLUGIN_LOAD_ERRORS):
        if stale_id not in enabled_ids:
            del PLUGIN_LOAD_ERRORS[stale_id]
    for plugin_id in plugin_settings.enabled:
        plugin_dir = root / plugin_id
        try:
            if not (plugin_dir / MANIFEST_FILENAME).is_file():
                raise PluginLoadError(plugin_id, "resolve", "插件未安装（缺少 manifest.json）")
            registration = _load_plugin_dir(
                plugin_id=plugin_id,
                plugin_dir=plugin_dir,
                plugin_settings=plugin_settings,
            )
            loaded.append(registration)
            PLUGIN_LOAD_ERRORS.pop(plugin_id, None)
        except PluginLoadError as exc:
            PLUGIN_LOAD_ERRORS[plugin_id] = {
                "stage": exc.stage,
                "message": str(exc),
            }
        except Exception as exc:
            PLUGIN_LOAD_ERRORS[plugin_id] = {
                "stage": "load",
                "message": str(exc),
            }
    return tuple(loaded)
