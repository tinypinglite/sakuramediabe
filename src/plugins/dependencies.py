"""插件运行时依赖管理器。

宿主托管安装：pip 装进插件私有 deps/ 目录，与宿主同名的包按"版本一致复用、
版本不一致拒绝"的策略处理，避免同进程双副本。安装过程原子切换、失败回滚，
api/aps 双进程通过文件锁串行化。
"""

from __future__ import annotations

import csv
import fcntl
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from src.plugins.manifest import PluginManifest

DEPS_DIRNAME = "deps"
INSTALLED_JSON = "installed.json"
INSTALL_LOG = "install.log"
INSTALLED_JSON_SCHEMA_VERSION = 1
DEFAULT_INSTALL_TIMEOUT_SECONDS = 600
INSTALL_LOCK_FILENAME = ".install.lock"


class PluginDependencyError(RuntimeError):
    """依赖安装或校验失败；message 需要能直接展示给用户。"""


def host_distributions() -> dict[str, str]:
    """宿主已安装分发物：{规范化包名: 版本}。"""
    return {
        canonicalize_name(dist.metadata.get("Name") or ""): dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    }


def target_distributions(target: Path) -> dict[str, str]:
    """target 目录（pip --target 产物）中的分发物：{规范化包名: 版本}。"""
    return {
        canonicalize_name(dist.metadata.get("Name") or ""): dist.version
        for dist in importlib.metadata.distributions(path=[str(target)])
        if dist.metadata.get("Name")
    }


def _host_satisfies(requirement: Requirement, host_dists: dict[str, str]) -> bool:
    version = host_dists.get(canonicalize_name(requirement.name))
    if version is None:
        return False
    return requirement.specifier.contains(version, prereleases=True)


def _log_append(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _remove_dist_from_target(dist: importlib.metadata.Distribution, target: Path) -> None:
    """从 pip --target 目录删除一个分发物（含 RECORD 列出的文件与 dist-info）。"""
    dist_info = Path(dist._path)
    record_path = dist_info / "RECORD"
    if not record_path.is_file():
        # 缺少 RECORD 时保守处理：整目录不可靠，直接报错由上层判定冲突。
        raise PluginDependencyError(
            f"无法清理宿主重复包 {dist.metadata['Name']}: 缺少 RECORD"
        )
    with record_path.open(encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if not row or not row[0]:
                continue
            relative = row[0]
            file_path = (target / relative).resolve()
            if target.resolve() not in file_path.parents:
                raise PluginDependencyError(
                    f"依赖包 RECORD 包含越界路径: {relative}"
                )
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
    # 删除可能的空目录（从 dist-info 向上清理到 target 为止）。
    for directory in sorted(
        {file_path.parent for file_path in _iter_dist_files(dist_info)},
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            if target.resolve() in directory.resolve().parents:
                directory.rmdir()
        except OSError:
            continue
    shutil.rmtree(dist_info, ignore_errors=True)


def _iter_dist_files(dist_info: Path):
    """遍历 dist-info 下 RECORD 引用的文件，供空目录清理使用。"""
    record_path = dist_info / "RECORD"
    if not record_path.is_file():
        return
    with record_path.open(encoding="utf-8") as handle:
        for row in csv.reader(handle):
            if row and row[0]:
                yield dist_info.parent / row[0]


def _python_platform() -> str:
    return sysconfig_platform()


def sysconfig_platform() -> str:
    import sysconfig

    return sysconfig.get_platform()


def _pip_install(
    *,
    target: Path,
    requirements: list[str],
    manifest: PluginManifest,
    plugin_dir: Path,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> None:
    """执行 pip 安装并把完整输出追加到 install.log。"""
    log_path = plugin_dir / INSTALL_LOG
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--target",
        str(target),
    ]
    wheels_dir = plugin_dir / "wheels"
    if manifest.dependencies.bundled_wheels and wheels_dir.is_dir():
        command.extend(["--find-links", str(wheels_dir)])
    if manifest.dependencies.index_url:
        command.extend(["--index-url", manifest.dependencies.index_url])
    for index_url in manifest.dependencies.extra_index_urls:
        command.extend(["--extra-index-url", index_url])
    command.extend(requirements)

    _log_append(
        log_path,
        f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] pip install: "
        + " ".join(command)
        + "\n",
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log_append(log_path, f"pip 安装超时（>{timeout}s）\n")
        raise PluginDependencyError(f"依赖安装超时（>{timeout}s）") from exc
    _log_append(log_path, completed.stdout)
    _log_append(log_path, completed.stderr)
    if completed.returncode != 0:
        raise PluginDependencyError(
            f"pip 安装失败（exit={completed.returncode}），详见 install.log"
        )


def _prune_host_duplicates(target: Path, host_dists: dict[str, str]) -> dict[str, str]:
    """宿主同名包：版本一致则复用宿主并从插件目录剔除，不一致直接失败。"""
    remaining: dict[str, str] = {}
    for dist in importlib.metadata.distributions(path=[str(target)]):
        name = canonicalize_name(dist.metadata.get("Name") or "")
        if not name:
            continue
        version = dist.version
        host_version = host_dists.get(name)
        if host_version is None:
            remaining[name] = version
            continue
        if host_version == version:
            _remove_dist_from_target(dist, target)
            continue
        raise PluginDependencyError(
            f"依赖与宿主包 {dist.metadata['Name']} 版本冲突: "
            f"宿主={host_version} 插件={version}；插件不能覆盖宿主依赖"
        )
    return remaining


def _verify_requirements(
    requirements: list[str],
    host_dists: dict[str, str],
    plugin_dists: dict[str, str],
    plugin_id: str,
) -> None:
    """安装完成后校验全部直接需求由宿主或插件目录满足。"""
    for requirement_str in requirements:
        requirement = Requirement(requirement_str)
        if _host_satisfies(requirement, host_dists):
            continue
        version = plugin_dists.get(canonicalize_name(requirement.name))
        if version is None or not requirement.specifier.contains(
            version, prereleases=True
        ):
            raise PluginDependencyError(
                f"插件 {plugin_id} 依赖 {requirement_str} 安装后仍不满足"
            )


def _write_installed_json(
    plugin_dir: Path,
    manifest: PluginManifest,
    dists: dict[str, str],
) -> None:
    payload = {
        "schema_version": INSTALLED_JSON_SCHEMA_VERSION,
        "plugin_id": manifest.plugin_id,
        "version": manifest.version,
        "manifest_dependencies_digest": manifest.dependencies_digest(),
        "platform": sysconfig_platform(),
        "python_version": platform.python_version(),
        "dists": dists,
        "index_url": manifest.dependencies.index_url,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = plugin_dir / f"{INSTALLED_JSON}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, plugin_dir / INSTALLED_JSON)


def _read_installed_json(plugin_dir: Path) -> dict[str, Any] | None:
    path = plugin_dir / INSTALLED_JSON
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


class InstallLock:
    """跨进程（api/aps）依赖安装互斥锁。"""

    def __init__(self, root_dir: Path):
        self.lock_path = root_dir / INSTALL_LOCK_FILENAME

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def install_plugin_dependencies(
    plugin_dir: Path,
    manifest: PluginManifest,
    *,
    root_dir: Path,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """把插件依赖装进 ``plugin_dir/deps``（原子切换、失败回滚）。

    返回最终留在插件目录的分发物 {name: version}（宿主同版本包已被剔除）。
    """
    with InstallLock(root_dir):
        return _install_plugin_dependencies_locked(
            plugin_dir,
            manifest,
            timeout=timeout,
        )


def _install_plugin_dependencies_locked(
    plugin_dir: Path,
    manifest: PluginManifest,
    *,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """依赖安装主体；调用方必须已持有 InstallLock。"""
    host_dists = host_distributions()
    remaining_requirements = [
        requirement_str
        for requirement_str in manifest.dependencies.requirements
        if not _host_satisfies(Requirement(requirement_str), host_dists)
    ]

    deps_dir = plugin_dir / DEPS_DIRNAME
    staging = plugin_dir / f".{DEPS_DIRNAME}-staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    final_dists: dict[str, str] = {}
    try:
        if remaining_requirements:
            _pip_install(
                target=staging,
                requirements=remaining_requirements,
                manifest=manifest,
                plugin_dir=plugin_dir,
                timeout=timeout,
            )
            final_dists = _prune_host_duplicates(staging, host_dists)
            _verify_requirements(
                manifest.dependencies.requirements,
                host_dists,
                final_dists,
                manifest.plugin_id,
            )

        # 原子切换：旧 deps 先改名保留，切换失败可恢复。
        old_backup: Path | None = None
        if deps_dir.exists():
            old_backup = plugin_dir / f".{DEPS_DIRNAME}-old-{int(time.time())}"
            if old_backup.exists():
                shutil.rmtree(old_backup, ignore_errors=True)
            os.replace(deps_dir, old_backup)
        try:
            if final_dists:
                os.replace(staging, deps_dir)
            else:
                # 全部复用宿主：不保留空 deps 目录，删掉半成品。
                shutil.rmtree(staging, ignore_errors=True)
                if deps_dir.exists():
                    shutil.rmtree(deps_dir, ignore_errors=True)
        except Exception:
            if (
                old_backup is not None
                and old_backup.exists()
                and not deps_dir.exists()
            ):
                os.replace(old_backup, deps_dir)
            raise
        if old_backup is not None and old_backup.exists():
            shutil.rmtree(old_backup, ignore_errors=True)

        _write_installed_json(plugin_dir, manifest, final_dists)
        return final_dists
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _installed_json_fresh(
    plugin_dir: Path,
    installed: dict[str, Any] | None,
    manifest: PluginManifest,
) -> bool:
    """installed.json 是否与当前声明一致，且 deps 目录真实存在。"""
    if installed is None:
        return False
    if installed.get("schema_version") != INSTALLED_JSON_SCHEMA_VERSION:
        return False
    if installed.get("manifest_dependencies_digest") != manifest.dependencies_digest():
        return False
    if installed.get("platform") != sysconfig_platform():
        return False
    if installed.get("python_version") != platform.python_version():
        return False
    # 记录有私有依赖时，deps/ 必须真实存在；缺失即视为待重装。
    return not (
        installed.get("dists") and not (plugin_dir / DEPS_DIRNAME).is_dir()
    )


def ensure_plugin_dependencies(
    plugin_dir: Path,
    manifest: PluginManifest,
    *,
    root_dir: Path,
    timeout: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """启动加载前确保依赖就绪：缺失/摘要/平台不符或 deps 目录丢失则重装。

    在锁内二次校验，避免 api/aps 同时冷启动时重复安装。
    """
    with InstallLock(root_dir):
        installed = _read_installed_json(plugin_dir)
        if _installed_json_fresh(plugin_dir, installed, manifest):
            return installed.get("dists") or {}
        return _install_plugin_dependencies_locked(
            plugin_dir,
            manifest,
            timeout=timeout,
        )


_INSERTED_DEPS_PATHS: set[str] = set()


def load_plugin_deps_into_syspath(plugin_dir: Path) -> None:
    """把插件 deps 目录插到 sys.path 最前（每个目录只插入一次）。"""
    deps_dir = plugin_dir / DEPS_DIRNAME
    if not deps_dir.is_dir():
        return
    key = str(deps_dir.resolve())
    if key in _INSERTED_DEPS_PATHS:
        return
    sys.path.insert(0, key)
    _INSERTED_DEPS_PATHS.add(key)


def plugin_dep_conflict_message(
    plugin_id: str,
    plugin_dir: Path,
    loaded_deps: dict[str, tuple[str, str]],
) -> str | None:
    """当前插件 deps 与已加载插件是否冲突；返回冲突说明或 None。"""
    deps_dir = plugin_dir / DEPS_DIRNAME
    if not deps_dir.is_dir():
        return None
    for name, version in target_distributions(deps_dir).items():
        previous = loaded_deps.get(name)
        if previous is not None:
            owner_id, owner_version = previous
            return (
                f"插件依赖冲突: {plugin_id} 与 {owner_id} 都提供 {name} "
                f"({owner_version} vs {version})"
            )
    return None
