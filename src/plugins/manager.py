"""插件生命周期管理编排：安装/升级/回滚/启停/卸载/依赖重装与状态查询。

文件系统操作由 PluginInstaller 负责，配置写入复用统一配置持久化，
本模块只做编排与状态汇总。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.config.config import Settings, settings, update_settings
from src.plugins.dependencies import (
    INSTALLED_JSON,
    install_plugin_dependencies,
)
from src.plugins.installer import InstallResult, PluginInstaller
from src.plugins.loader import PLUGIN_LOAD_ERRORS
from src.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest_from_file,
)


def _plugin_root() -> Path:
    return Path(settings.plugins.root_dir).expanduser()


class PluginManager:
    """插件管理入口；所有写操作返回 pending_restart（插件在 import 期加载）。"""

    def __init__(self, root_dir: Path | None = None):
        self.root_dir = Path(root_dir) if root_dir is not None else _plugin_root()
        self.installer = PluginInstaller(self.root_dir)

    # ---- 状态 ----

    def _enabled_ids(self) -> list[str]:
        return list(settings.plugins.enabled)

    @staticmethod
    def _load_manifest(
        plugin_dir: Path,
    ) -> tuple[PluginManifest | None, str | None]:
        """读取插件 manifest；损坏时返回 (None, 错误信息)，不向调用方抛异常。"""
        if not (plugin_dir / MANIFEST_FILENAME).is_file():
            return None, None
        try:
            return load_manifest_from_file(plugin_dir), None
        except ValueError as exc:
            return None, str(exc)

    def list_plugins(self) -> list[dict[str, Any]]:
        enabled_ids = set(self._enabled_ids())
        plugins: list[dict[str, Any]] = []
        if not self.root_dir.is_dir():
            return plugins
        for plugin_dir in sorted(self.root_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            if not (plugin_dir / MANIFEST_FILENAME).is_file():
                continue
            manifest, manifest_error = self._load_manifest(plugin_dir)
            plugin_id = plugin_dir.name if manifest is None else manifest.plugin_id
            if manifest is not None:
                display_name = manifest.display_name
                version = manifest.version
                host_api_version = manifest.host_api_version
                deps_status = self._deps_status(plugin_dir, manifest)
            else:
                display_name = plugin_id
                version = "unknown"
                host_api_version = 0
                deps_status = "unknown"
            plugins.append(
                {
                    "plugin_id": plugin_id,
                    "display_name": display_name,
                    "version": version,
                    "host_api_version": host_api_version,
                    "enabled": plugin_id in enabled_ids,
                    "deps_status": deps_status,
                    "installed_at": self._installed_at(plugin_dir),
                    "load_status": (
                        "error"
                        if manifest_error is not None
                        or plugin_id in PLUGIN_LOAD_ERRORS
                        else "ok"
                    ),
                    "load_error": (
                        manifest_error
                        or PLUGIN_LOAD_ERRORS.get(plugin_id, {}).get("message")
                    ),
                }
            )
        return plugins

    def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        plugin_dir = self.root_dir / plugin_id
        manifest, manifest_error = self._load_manifest(plugin_dir)
        if manifest is None and manifest_error is None:
            return None
        if manifest is None:
            plugin_id = plugin_dir.name
        installed = self._read_installed(plugin_dir)
        log_path = plugin_dir / "install.log"
        enabled = plugin_id in set(self._enabled_ids())
        return {
            "plugin_id": plugin_id,
            "display_name": manifest.display_name if manifest else plugin_id,
            "version": manifest.version if manifest else "unknown",
            "host_api_version": manifest.host_api_version if manifest else 0,
            "requires_python": manifest.requires_python if manifest else None,
            "author": manifest.author if manifest else None,
            "homepage": manifest.homepage if manifest else None,
            "dependencies": (
                manifest.dependencies.model_dump() if manifest else {}
            ),
            "manifest": manifest.model_dump() if manifest else {},
            "enabled": enabled,
            "deps_status": (
                self._deps_status(plugin_dir, manifest) if manifest else "unknown"
            ),
            "dists": installed.get("dists", {}) if installed else {},
            "installed_at": installed.get("installed_at") if installed else None,
            "data_dir": str(plugin_dir / "data"),
            "install_log_tail": self._log_tail(log_path, limit=2000),
            "load_status": (
                "error"
                if manifest_error is not None or plugin_id in PLUGIN_LOAD_ERRORS
                else "ok"
            ),
            "load_error": (
                manifest_error
                or PLUGIN_LOAD_ERRORS.get(plugin_id, {}).get("message")
            ),
        }

    @staticmethod
    def _read_installed(plugin_dir: Path) -> dict[str, Any] | None:
        path = plugin_dir / INSTALLED_JSON
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    def _deps_status(cls, plugin_dir: Path, manifest: PluginManifest) -> str:
        installed = cls._read_installed(plugin_dir)
        if installed is None:
            return "missing"
        if installed.get("dists") and not (plugin_dir / "deps").is_dir():
            return "missing"
        if (
            installed.get("manifest_dependencies_digest")
            != manifest.dependencies_digest()
        ):
            return "stale"
        return "ok"

    @staticmethod
    def _installed_at(plugin_dir: Path) -> str | None:
        installed = PluginManager._read_installed(plugin_dir)
        return installed.get("installed_at") if installed else None

    @staticmethod
    def _log_tail(log_path: Path, limit: int = 2000) -> str:
        if not log_path.is_file():
            return ""
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                content = handle.read()
        except OSError:
            return ""
        return content.decode("utf-8", errors="replace")

    # ---- 写操作 ----

    def install(
        self,
        zip_path: Path,
        *,
        sha256: str | None = None,
        enable: bool = True,
    ) -> InstallResult:
        result = self.installer.install(
            zip_path,
            sha256=sha256,
            plugin_settings=settings.plugins,
        )
        if enable:
            self._set_enabled(result.plugin_id, True)
        return result

    def update(
        self,
        plugin_id: str,
        zip_path: Path,
        *,
        sha256: str | None = None,
    ) -> InstallResult:
        return self.installer.update(
            plugin_id,
            zip_path,
            sha256=sha256,
            plugin_settings=settings.plugins,
        )

    def rollback(self, plugin_id: str) -> InstallResult:
        return self.installer.rollback(plugin_id)

    def reinstall_deps(self, plugin_id: str) -> None:
        plugin_dir = self.root_dir / plugin_id
        manifest = load_manifest_from_file(plugin_dir)
        # “重装依赖”是强制动作：即使 installed.json 看似最新也重新安装。
        install_plugin_dependencies(
            plugin_dir,
            manifest,
            root_dir=self.root_dir,
        )

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if not (self.root_dir / plugin_id / MANIFEST_FILENAME).is_file():
            raise ValueError(f"插件未安装: {plugin_id}")
        self._set_enabled(plugin_id, enabled)

    def _set_enabled(self, plugin_id: str, enabled: bool) -> None:
        current = Settings.model_validate(settings.model_dump())
        enabled_ids = list(current.plugins.enabled)
        if enabled and plugin_id not in enabled_ids:
            enabled_ids.append(plugin_id)
        elif not enabled and plugin_id in enabled_ids:
            enabled_ids.remove(plugin_id)
        if enabled_ids == current.plugins.enabled:
            return
        current.plugins.enabled = enabled_ids
        update_settings(current)

    def uninstall(self, plugin_id: str, *, purge_data: bool = False) -> InstallResult:
        self._set_enabled(plugin_id, False)
        return self.installer.uninstall(plugin_id, purge_data=purge_data)
