"""插件声明依赖的启动前同步与失败状态。"""

from __future__ import annotations

import json
import site
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

from src.config.config import Plugins
from src.i18n import translate
from src.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest_from_file,
)

_RUNTIME_DIRNAME = ".runtime"
_SITE_PACKAGES_DIRNAME = "site-packages"
_FAILURES_FILENAME = "dependency-failures.json"


def dependency_site_packages_dir(root_dir: Path) -> Path:
    """返回宿主托管、供全部插件共享的额外 site-packages 目录。"""
    return Path(root_dir) / _RUNTIME_DIRNAME / _SITE_PACKAGES_DIRNAME


def _failures_path(root_dir: Path) -> Path:
    return Path(root_dir) / _RUNTIME_DIRNAME / _FAILURES_FILENAME


def _read_failures(root_dir: Path) -> dict[str, dict[str, object]]:
    path = _failures_path(root_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        plugin_id: value
        for plugin_id, value in raw.items()
        if isinstance(plugin_id, str) and isinstance(value, dict)
    }


def _write_failures(root_dir: Path, failures: dict[str, dict[str, object]]) -> None:
    path = _failures_path(root_dir)
    if not failures:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(failures, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def dependency_failure_record(
    *,
    root_dir: Path,
    manifest: PluginManifest,
) -> dict[str, Any] | None:
    """返回仍匹配当前清单的依赖同步失败登记（含语言包 key）。"""
    failure = _read_failures(root_dir).get(manifest.plugin_id)
    if (
        failure is None
        or failure.get("dependencies") != manifest.dependencies
        or not isinstance(failure.get("message"), str)
    ):
        return None
    return failure


def render_dependency_failure(failure: dict[str, Any]) -> str:
    """按当前语言渲染失败原因；旧登记缺少 key 时返回落盘的中文原文。"""
    message = failure.get("message")
    message_key = failure.get("message_key")
    if not isinstance(message, str):
        return ""
    if not isinstance(message_key, str):
        return message
    params = failure.get("params")
    return translate(
        message_key,
        default=message,
        **(params if isinstance(params, dict) else {}),
    )


def dependency_failure_message(
    *,
    root_dir: Path,
    manifest: PluginManifest,
) -> str | None:
    """返回仍匹配当前清单的依赖同步失败原因，按当前语言渲染。"""
    failure = dependency_failure_record(root_dir=root_dir, manifest=manifest)
    return None if failure is None else render_dependency_failure(failure)


def enable_dependency_site_packages(root_dir: Path) -> None:
    """让随后导入的插件可见其额外依赖，同时保持宿主依赖优先。"""
    site_packages = dependency_site_packages_dir(root_dir)
    if site_packages.is_dir():
        site.addsitedir(str(site_packages))


def _dependency_failure(
    message_key: str,
    message: str,
    **params: Any,
) -> dict[str, Any]:
    """构造失败登记：中文原文落盘兜底，key+params 供响应时按语言渲染。"""
    return {"message_key": message_key, "message": message, "params": params}


def _install_dependencies(
    *, site_packages: Path, dependencies: list[str]
) -> dict[str, Any] | None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--target",
            str(site_packages),
            *dependencies,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    details = (result.stderr or result.stdout).strip().splitlines()
    suffix = details[-1] if details else f"pip 退出码 {result.returncode}"
    return _dependency_failure(
        "plugins.dependency.install_failed",
        f"依赖安装失败: {suffix}",
        detail=suffix,
    )


def _validate_effective_dependencies(
    *,
    root_dir: Path,
    dependencies: list[str],
) -> dict[str, Any] | None:
    """确认当前 Python 实际会解析到的版本满足插件声明。"""
    enable_dependency_site_packages(root_dir)
    for value in dependencies:
        requirement = Requirement(value)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed_version = version(requirement.name)
        except PackageNotFoundError:
            return _dependency_failure(
                "plugins.dependency.package_missing",
                f"依赖安装失败: 未找到已安装包 {requirement.name}",
                package=requirement.name,
            )
        if requirement.specifier and not requirement.specifier.contains(
            installed_version,
            prereleases=True,
        ):
            return _dependency_failure(
                "plugins.dependency.version_unsatisfied",
                "依赖安装失败: "
                f"已生效的 {requirement.name}={installed_version} "
                f"不满足 {value}",
                package=requirement.name,
                version=installed_version,
                requirement=value,
            )
    return None


def _has_missing_dependencies(*, root_dir: Path, dependencies: list[str]) -> bool:
    enable_dependency_site_packages(root_dir)
    for value in dependencies:
        requirement = Requirement(value)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            version(requirement.name)
        except PackageNotFoundError:
            return True
    return False


def sync_plugin_dependencies(
    plugin_settings: Plugins,
    *,
    root_dir: Path,
) -> dict[str, str]:
    """为启用且声明依赖的插件同步依赖，返回每个失败插件的错误。"""
    root_dir = Path(root_dir)
    site_packages = dependency_site_packages_dir(root_dir)
    failures: dict[str, dict[str, Any]] = {}
    manifests: dict[str, PluginManifest] = {}

    for plugin_id in plugin_settings.enabled:
        plugin_dir = root_dir / plugin_id
        if not (plugin_dir / MANIFEST_FILENAME).is_file():
            continue
        try:
            manifest = load_manifest_from_file(plugin_dir)
        except ValueError:
            continue
        if manifest.plugin_id != plugin_id or not manifest.dependencies:
            continue
        manifests[plugin_id] = manifest
        if not _has_missing_dependencies(
            root_dir=root_dir,
            dependencies=manifest.dependencies,
        ):
            continue
        try:
            site_packages.mkdir(parents=True, exist_ok=True)
            failure = _install_dependencies(
                site_packages=site_packages,
                dependencies=manifest.dependencies,
            )
        except OSError as exc:
            failure = _dependency_failure(
                "plugins.dependency.install_failed",
                f"依赖安装失败: {exc}",
                detail=str(exc),
            )
        if failure is not None:
            failures[plugin_id] = {
                "dependencies": manifest.dependencies,
                **failure,
            }

    for plugin_id, manifest in manifests.items():
        if plugin_id in failures:
            continue
        failure = _validate_effective_dependencies(
            root_dir=root_dir,
            dependencies=manifest.dependencies,
        )
        if failure is not None:
            failures[plugin_id] = {
                "dependencies": manifest.dependencies,
                **failure,
            }

    _write_failures(root_dir, failures)
    return {
        plugin_id: render_dependency_failure(failure)
        for plugin_id, failure in failures.items()
    }
