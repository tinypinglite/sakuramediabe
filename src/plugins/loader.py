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

from pydantic import BaseModel

from src.config.config import Plugins
from src.i18n import translate
from src.plugins.context import PluginContext
from src.plugins.contracts import (
    HOST_API_VERSION,
    MIN_SUPPORTED_HOST_API_VERSION,
    PluginRegistration,
    validate_host_api_version,
)
from src.plugins.dependencies import (
    dependency_failure_record,
    enable_dependency_site_packages,
    render_dependency_failure,
)
from src.plugins.extensions import EXTENSION_VALIDATORS, REPEATABLE_EXTENSION_KEYS
from src.plugins.extensions.metadata import (
    METADATA_SOURCE_EXTENSION_KEY,
    METADATA_SOURCE_HOST_API_VERSION,
)
from src.plugins.extensions.ranking import (
    RANKING_MULTI_SOURCE_HOST_API_VERSION,
    RANKING_SOURCE_EXTENSION_KEY,
)
from src.plugins.extensions.reviews import (
    REVIEWS_EXTENSION_KEY,
    REVIEWS_HOST_API_VERSION,
)
from src.plugins.extensions.stats import (
    STATS_SOURCE_EXTENSION_KEY,
    STATS_SOURCE_HOST_API_VERSION,
)
from src.plugins.i18n import validate_plugin_catalogs
from src.plugins.manifest import MANIFEST_FILENAME, load_manifest_from_file
from src.plugins.provider_protocol import refresh_media_provider_registry
from src.scheduler.contracts import JobDefinition

PLUGIN_MODULE_NAMESPACE = "sakuramedia_plugins"

# 插件加载错误登记：坏插件隔离，不拖垮整个服务；CLI 据此展示。
PLUGIN_LOAD_ERRORS: dict[str, dict[str, Any]] = {}

LOAD_ERROR_SUMMARY_KEY = "plugins.load_error.summary"


class PluginLoadError(RuntimeError):
    """已显式启用的插件无法完成加载。

    ``message`` 是中文原文（日志与 CLI 使用）；``message_key``/``message_params``
    指向宿主语言包，插件管理页在响应时按请求语言渲染。
    """

    def __init__(
        self,
        plugin_id: str,
        stage: str,
        message: str,
        *,
        message_key: str | None = None,
        message_params: dict[str, Any] | None = None,
    ):
        self.plugin_id = plugin_id
        self.stage = stage
        self.reason = message
        self.message_key = message_key
        self.message_params = dict(message_params or {})
        super().__init__(f"插件加载失败 plugin_id={plugin_id} stage={stage}: {message}")


def _load_error(
    plugin_id: str,
    stage: str,
    message_key: str | None,
    reason: str,
    **params: Any,
) -> PluginLoadError:
    """构造带语言包 key 的加载错误；``reason`` 保留中文原文兜底。"""
    return PluginLoadError(
        plugin_id,
        stage,
        reason,
        message_key=message_key,
        message_params=params,
    )


def _load_error_entry(error: PluginLoadError) -> dict[str, Any]:
    """登记失败插件；``message`` 供日志/CLI 直接展示。"""
    return {
        "stage": error.stage,
        "plugin_id": error.plugin_id,
        "message": str(error),
        "reason": error.reason,
        "message_key": error.message_key,
        "message_params": error.message_params,
    }


def render_plugin_load_error(entry: dict[str, Any]) -> str:
    """按当前语言渲染加载错误登记项。

    调度器侧登记的条目和未知异常没有语言包 key，原样返回中文 message。
    """
    message = str(entry.get("message", ""))
    message_key = entry.get("message_key")
    if not isinstance(message_key, str):
        return message
    params = entry.get("message_params")
    reason = translate(
        message_key,
        default=str(entry.get("reason") or message),
        **(params if isinstance(params, dict) else {}),
    )
    summary = translate(LOAD_ERROR_SUMMARY_KEY, default="插件加载失败")
    return f"{summary} plugin_id={entry.get('plugin_id')} stage={entry.get('stage')}: {reason}"


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
        raise _load_error(
            plugin_id,
            "import",
            "plugins.load_error.module_spec_failed",
            "无法创建插件模块 spec",
        )
    module = importlib.util.module_from_spec(spec)
    # 先注册再 exec，保证插件内部的相对导入（from .settings import ...）可解析。
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _clear_plugin_modules(plugin_id)
        raise
    return module


def load_plugin_settings_model(plugin_dir: Path) -> type[BaseModel] | None:
    """读取包根声明的配置模型，不执行 register，也不校验当前配置。"""
    manifest = load_manifest_from_file(plugin_dir)
    if manifest.settings_model is None:
        return None
    validate_host_api_version(manifest.host_api_version)
    enable_dependency_site_packages(plugin_dir.parent)
    module = sys.modules.get(f"{PLUGIN_MODULE_NAMESPACE}.{manifest.plugin_id}")
    if module is None:
        module = _import_plugin_package(plugin_dir, manifest.plugin_id)
    model = getattr(module, manifest.settings_model, None)
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("settings_model 必须指向包根导出的 Pydantic 配置类")
    return model


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
        raise _load_error(
            plugin_id,
            "validate_jobs",
            "plugins.load_error.job_task_key_duplicate",
            "插件内部 task_key 重复",
        )

    jobs: list[JobDefinition] = []
    for job in registration.jobs:
        if job.plugin_id is not None:
            raise _load_error(
                plugin_id,
                "validate_jobs",
                "plugins.load_error.job_plugin_id_forbidden",
                f"任务 {job.task_key} 不允许自行声明 plugin_id",
                task_key=job.task_key,
            )
        if job.cron_setting is not None:
            raise _load_error(
                plugin_id,
                "validate_jobs",
                "plugins.load_error.job_cron_setting_forbidden",
                f"任务 {job.task_key} 不允许引用宿主 Scheduler 字段",
                task_key=job.task_key,
            )
        jobs.append(job.model_copy(update={"plugin_id": plugin_id}))
    return tuple(jobs)


def _validate_plugin_extensions(
    *,
    plugin_id: str,
    registration: PluginRegistration,
) -> None:
    """扩展点声明通用校验：key 受宿主支持、非重复扩展点 key 唯一，并委托领域校验器。

    这里不感知任何领域语义；data 的形状与约束由按 key 注册的校验器解释，
    失败统一走 PluginLoadError，保持坏插件隔离。
    """
    seen: set[str] = set()
    for extension in registration.extensions:
        if (
            extension.key in seen
            and extension.key not in REPEATABLE_EXTENSION_KEYS
        ):
            raise _load_error(
                plugin_id,
                "validate_extensions",
                "plugins.load_error.extension_key_duplicate",
                f"扩展点 key 重复: {extension.key}",
                extension_key=extension.key,
            )
        seen.add(extension.key)
        validator = EXTENSION_VALIDATORS.get(extension.key)
        if validator is None:
            raise _load_error(
                plugin_id,
                "validate_extensions",
                "plugins.load_error.extension_unsupported",
                f"宿主不支持该扩展点: {extension.key}",
                extension_key=extension.key,
            )
        try:
            validator(plugin_id=plugin_id, extension=extension)
        except Exception as exc:
            raise _load_error(
                plugin_id,
                "validate_extensions",
                "plugins.load_error.extension_validation_failed",
                f"扩展点 {extension.key} 校验失败: {exc}",
                extension_key=extension.key,
                error=str(exc),
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
        raise _load_error(
            plugin_id,
            "resolve",
            "plugins.load_error.manifest_plugin_id_mismatch",
            f"manifest.plugin_id={manifest.plugin_id} 与启用项不一致",
            manifest_plugin_id=manifest.plugin_id,
        )
    try:
        validate_host_api_version(manifest.host_api_version)
    except ValueError as exc:
        raise _load_error(
            plugin_id,
            "validate_manifest",
            "plugins.load_error.host_api_incompatible",
            str(exc),
            version=manifest.host_api_version,
            min_version=MIN_SUPPORTED_HOST_API_VERSION,
            max_version=HOST_API_VERSION,
        ) from exc
    dependency_failure = dependency_failure_record(
        root_dir=plugin_dir.parent,
        manifest=manifest,
    )
    if dependency_failure is not None:
        reason = render_dependency_failure(dependency_failure)
        failure_key = dependency_failure.get("message_key")
        failure_params = dependency_failure.get("params")
        raise _load_error(
            plugin_id,
            "dependencies",
            failure_key if isinstance(failure_key, str) else None,
            reason,
            **(failure_params if isinstance(failure_params, dict) else {}),
        )
    enable_dependency_site_packages(plugin_dir.parent)

    try:
        module = _import_plugin_package(plugin_dir, plugin_id)
    except Exception as exc:
        raise _load_error(
            plugin_id,
            "import",
            "plugins.load_error.import_failed",
            f"无法导入插件包: {exc}",
            error=str(exc),
        ) from exc

    register = getattr(module, "register", None)
    if not callable(register):
        raise _load_error(
            plugin_id,
            "resolve_register",
            "plugins.load_error.register_missing",
            "插件包根未暴露可调用的 register(context)",
        )

    context = PluginContext(
        plugin_id=plugin_id,
        settings=_freeze_settings(
            deepcopy(plugin_settings.settings.get(plugin_id, {}))
        ),
        data_dir=plugin_dir / "data",
        plugin_root=plugin_dir,
    )
    context.ensure_data_dir()
    try:
        registration = PluginRegistration.model_validate(register(context))
    except Exception as exc:
        if isinstance(exc, PluginLoadError):
            raise
        raise _load_error(
            plugin_id,
            "register",
            "plugins.load_error.register_failed",
            f"register(context) 执行或返回值校验失败: {exc}",
            error=str(exc),
        ) from exc

    if registration.plugin_id != plugin_id:
        raise _load_error(
            plugin_id,
            "validate_registration",
            "plugins.load_error.registration_plugin_id_mismatch",
            f"register 返回的 plugin_id={registration.plugin_id} 与配置不一致",
            returned_plugin_id=registration.plugin_id,
        )
    if registration.version != manifest.version:
        raise _load_error(
            plugin_id,
            "validate_registration",
            "plugins.load_error.registration_version_mismatch",
            "register 返回的 version 与 manifest 不一致: "
            f"register={registration.version} manifest={manifest.version}",
            register_version=registration.version,
            manifest_version=manifest.version,
        )
    # Legacy packages exist in both forms: some retain their v4 registration,
    # while the official plugins import the host's current constant at
    # runtime.  The manifest remains the pre-import compatibility boundary.
    supported_registration_versions = {
        manifest.host_api_version,
        HOST_API_VERSION,
    }
    if registration.host_api_version not in supported_registration_versions:
        raise _load_error(
            plugin_id,
            "validate_registration",
            "plugins.load_error.registration_host_api_mismatch",
            "register 返回的 host_api_version 与 manifest/宿主版本不兼容: "
            f"register={registration.host_api_version} "
            f"manifest={manifest.host_api_version} host={HOST_API_VERSION}",
            register_version=registration.host_api_version,
            manifest_version=manifest.host_api_version,
            host_version=HOST_API_VERSION,
        )

    if (
        any(ext.key == METADATA_SOURCE_EXTENSION_KEY for ext in registration.extensions)
        and manifest.host_api_version < METADATA_SOURCE_HOST_API_VERSION
    ):
        raise _load_error(
            plugin_id,
            "validate_extensions",
            "plugins.load_error.metadata_source_host_api_too_low",
            "元数据来源插件的 manifest 必须声明 Host API "
            f"{METADATA_SOURCE_HOST_API_VERSION} 或更高版本",
            min_version=METADATA_SOURCE_HOST_API_VERSION,
        )
    if (
        any(ext.key == REVIEWS_EXTENSION_KEY for ext in registration.extensions)
        and manifest.host_api_version < REVIEWS_HOST_API_VERSION
    ):
        raise _load_error(
            plugin_id,
            "validate_extensions",
            "plugins.load_error.movie_reviews_host_api_too_low",
            "电影评论插件的 manifest 必须声明 Host API "
            f"{REVIEWS_HOST_API_VERSION} 或更高版本",
            min_version=REVIEWS_HOST_API_VERSION,
        )
    if (
        any(ext.key == STATS_SOURCE_EXTENSION_KEY for ext in registration.extensions)
        and manifest.host_api_version < STATS_SOURCE_HOST_API_VERSION
    ):
        raise _load_error(
            plugin_id,
            "validate_extensions",
            "plugins.load_error.movie_stats_host_api_too_low",
            "互动数来源插件的 manifest 必须声明 Host API "
            f"{STATS_SOURCE_HOST_API_VERSION} 或更高版本",
            min_version=STATS_SOURCE_HOST_API_VERSION,
        )
    if (
        sum(
            1
            for ext in registration.extensions
            if ext.key == RANKING_SOURCE_EXTENSION_KEY
        )
        > 1
        and manifest.host_api_version < RANKING_MULTI_SOURCE_HOST_API_VERSION
    ):
        raise _load_error(
            plugin_id,
            "validate_extensions",
            "plugins.load_error.ranking_multi_source_host_api_too_low",
            "声明多个排行榜来源的插件 manifest 必须声明 Host API "
            f"{RANKING_MULTI_SOURCE_HOST_API_VERSION} 或更高版本",
            min_version=RANKING_MULTI_SOURCE_HOST_API_VERSION,
        )

    jobs = _validate_plugin_jobs(
        plugin_id=plugin_id,
        registration=registration,
    )
    _validate_plugin_extensions(
        plugin_id=plugin_id,
        registration=registration,
    )
    return registration.model_copy(update={
        "jobs": jobs, "host_api_version": manifest.host_api_version,
        # 插件可以只在 manifest 或 registration 里声明展示名翻译；manifest 是
        # 安装期就能读到的兼容边界，registration 未提供时用它补全，两边都有
        # 时以 registration 为准。
        "display_name_i18n": {
            **manifest.display_name_i18n,
            **registration.display_name_i18n,
        },
    })


def check_plugin_dir(
    *,
    plugin_dir: Path,
    plugin_settings: Plugins | None = None,
) -> PluginRegistration:
    """校验一个插件目录（import + register + 契约 + 语言包结构）。

    供 ``plugins check`` 与插件包安装前的试加载共用。插件 ID 取目录名；
    失败抛 PluginLoadError。校验后清理该插件命名空间的 sys.modules 副作用，
    避免污染后续加载。
    """
    plugin_dir = Path(plugin_dir)
    plugin_id = plugin_dir.name
    try:
        registration = _load_plugin_dir(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            plugin_settings=plugin_settings or Plugins(),
        )
        # 语言包只在这条试加载路径上严格校验；运行时 PluginI18n 宽松加载，
        # 坏语言包不会阻断已启用插件。错误明细走宿主语言包，已按当前请求语言
        # 渲染；总结文案带 message_key，响应侧可再按语言渲染。
        catalog_errors = validate_plugin_catalogs(plugin_dir)
        if catalog_errors:
            raise PluginLoadError(
                plugin_id,
                "i18n",
                "; ".join(catalog_errors),
                message_key="plugins.load_error.i18n_catalog_invalid",
                message_params={"errors": "; ".join(catalog_errors)},
            )
        return registration
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
                raise _load_error(
                    plugin_id,
                    "resolve",
                    "plugins.load_error.not_installed",
                    "插件未安装（缺少 manifest.json）",
                )
            registration = _load_plugin_dir(
                plugin_id=plugin_id,
                plugin_dir=plugin_dir,
                plugin_settings=plugin_settings,
            )
            loaded.append(registration)
            PLUGIN_LOAD_ERRORS.pop(plugin_id, None)
        except PluginLoadError as exc:
            _clear_plugin_modules(plugin_id)
            PLUGIN_LOAD_ERRORS[plugin_id] = _load_error_entry(exc)
        except Exception as exc:
            _clear_plugin_modules(plugin_id)
            # 未分类异常没有稳定文案，保留原始文本（可能包含插件自己的报错）。
            PLUGIN_LOAD_ERRORS[plugin_id] = {
                "stage": "load",
                "message": str(exc),
            }
    rejected_provider_plugins = refresh_media_provider_registry(loaded)
    for rejected_plugin_id in rejected_provider_plugins:
        # 注册表冲突原因只面向插件作者/日志，没有稳定的宿主文案 key，保留原文。
        PLUGIN_LOAD_ERRORS[rejected_plugin_id] = {
            "stage": "provider_registry",
            "message": "media.provider provider_key 与已加载插件重复",
        }
    return tuple(
        registration
        for registration in loaded
        if registration.plugin_id not in rejected_provider_plugins
    )
