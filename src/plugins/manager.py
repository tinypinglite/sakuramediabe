"""插件管理编排：目录/zip 安装、移除、启停与状态查询。

插件就是插件根目录下的一个子目录（含 manifest.json + __init__.py）：
安装 = 拷贝目录或解压 zip；移除 = 删除目录；启停 = 写配置 enabled 列表。
没有依赖托管、回滚与回收站——升级前请自行备份目录。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from src.config.config import Settings, settings, update_settings
from src.plugins.installer import PluginInstaller, PluginInstallError
from src.plugins.loader import PLUGIN_LOAD_ERRORS, PluginLoadError, check_plugin_dir
from src.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest_from_file,
)


def _plugin_root() -> Path:
    return Path(settings.plugins.root_dir).expanduser()


class PluginManager:
    """插件管理入口；启停写配置，目录操作即时生效但需重启加载。"""

    def __init__(self, root_dir: Path | None = None):
        self.root_dir = Path(root_dir) if root_dir is not None else _plugin_root()

    def _enabled_ids(self) -> list[str]:
        return list(settings.plugins.enabled)

    def _plugin_dir(self, plugin_id: str) -> Path:
        return self.root_dir / plugin_id

    @staticmethod
    def _load_manifest(
        plugin_dir: Path,
    ) -> tuple[PluginManifest | None, str | None]:
        """读取 manifest；损坏时返回 (None, 错误信息)，不向调用方抛异常。"""
        if not (plugin_dir / MANIFEST_FILENAME).is_file():
            return None, None
        try:
            return load_manifest_from_file(plugin_dir), None
        except ValueError as exc:
            return None, str(exc)

    # ---- 状态 ----

    def list_plugins(self) -> list[dict[str, Any]]:
        enabled_ids = set(self._enabled_ids())
        plugins: list[dict[str, Any]] = []
        if not self.root_dir.is_dir():
            return plugins
        for plugin_dir in sorted(self.root_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            manifest, manifest_error = self._load_manifest(plugin_dir)
            if manifest is None and manifest_error is None:
                continue
            plugin_id = plugin_dir.name if manifest is None else manifest.plugin_id
            plugins.append(
                {
                    "plugin_id": plugin_id,
                    "display_name": (
                        manifest.display_name if manifest is not None else plugin_id
                    ),
                    "version": manifest.version if manifest is not None else "unknown",
                    "host_api_version": (
                        manifest.host_api_version if manifest is not None else 0
                    ),
                    "enabled": plugin_id in enabled_ids,
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
        plugin_dir = self._plugin_dir(plugin_id)
        manifest, manifest_error = self._load_manifest(plugin_dir)
        if manifest is None and manifest_error is None:
            return None
        if manifest is None:
            plugin_id = plugin_dir.name
        return {
            "plugin_id": plugin_id,
            "display_name": manifest.display_name if manifest else plugin_id,
            "version": manifest.version if manifest else "unknown",
            "host_api_version": manifest.host_api_version if manifest else 0,
            "requires_python": manifest.requires_python if manifest else None,
            "author": manifest.author if manifest else None,
            "homepage": manifest.homepage if manifest else None,
            "manifest": manifest.model_dump() if manifest else {},
            "enabled": plugin_id in set(self._enabled_ids()),
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
            "data_dir": str(plugin_dir / "data"),
        }

    # ---- 写操作 ----

    def install(self, source_dir: Path, *, enable: bool = True) -> dict[str, str]:
        """把插件目录拷贝进插件根目录；目标已存在时替换代码并保留 data/。"""
        source_dir = Path(source_dir)
        if not source_dir.is_dir():
            raise ValueError(f"插件目录不存在: {source_dir}")
        manifest = load_manifest_from_file(source_dir)
        staging = self._staging_dir(manifest.plugin_id)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source_dir, staging)
        return self._publish_staging(staging, manifest, enable=enable)

    def install_zip(
        self,
        zip_path: Path,
        *,
        sha256: str | None = None,
        enable: bool = True,
    ) -> dict[str, str]:
        """解压 zip 并发布到插件根目录；目标已存在时替换代码并保留 data/。"""
        manifest, staging = PluginInstaller(self.root_dir).unpack(
            zip_path, sha256=sha256
        )
        try:
            # 发布前试加载：坏插件在安装期就被拒绝，而不是留到下次启动才报错。
            check_plugin_dir(
                plugin_dir=staging,
                plugin_settings=settings.plugins,
            )
        except PluginLoadError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise PluginInstallError(manifest.plugin_id, exc.stage, str(exc)) from exc
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self._publish_staging(staging, manifest, enable=enable)

    def _staging_dir(self, plugin_id: str) -> Path:
        return self.root_dir / ".staging" / plugin_id

    def _publish_staging(
        self,
        staging: Path,
        manifest: PluginManifest,
        *,
        enable: bool,
    ) -> dict[str, str]:
        """把暂存目录原子发布为正式插件目录，重复安装保留已有 data/。"""
        plugin_id = manifest.plugin_id
        target = self._plugin_dir(plugin_id)
        try:
            # data/ 是宿主托管目录：丢弃源目录自带 data，已有安装则沿用旧数据。
            if (staging / "data").exists():
                shutil.rmtree(staging / "data", ignore_errors=True)
            if target.is_dir():
                old_data = target / "data"
                if old_data.is_dir():
                    shutil.move(str(old_data), str(staging / "data"))
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(staging), str(target))
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"插件目录发布失败: {plugin_id}") from exc
        if enable:
            self._set_enabled(plugin_id, True)
        return {"plugin_id": plugin_id, "version": manifest.version}

    def remove(self, plugin_id: str) -> None:
        """删除插件目录（含 data/）；如需保留数据请先自行备份。"""
        target = self._plugin_dir(plugin_id)
        if not target.is_dir():
            raise ValueError(f"插件未安装: {plugin_id}")
        self._set_enabled(plugin_id, False)
        shutil.rmtree(target, ignore_errors=True)

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if not (self._plugin_dir(plugin_id) / MANIFEST_FILENAME).is_file():
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
