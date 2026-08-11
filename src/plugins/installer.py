"""插件安装器：zip 安全解压、完整性校验、依赖安装、原子发布、回滚与回收站。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.config.config import Plugins
from src.plugins.dependencies import install_plugin_dependencies
from src.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest_from_dict,
    load_manifest_from_file,
)

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


class PluginInstallError(RuntimeError):
    """安装/升级/回滚/卸载失败；stage 标识失败环节。"""

    def __init__(self, plugin_id: str, stage: str, message: str):
        self.plugin_id = plugin_id
        self.stage = stage
        super().__init__(f"插件{stage}失败 plugin_id={plugin_id}: {message}")


@dataclass(frozen=True)
class InstallResult:
    plugin_id: str
    version: str
    action: str
    pending_restart: tuple[str, ...] = ("api", "aps")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest_from_zip(zip_path: Path) -> PluginManifest:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            raw = archive.read(MANIFEST_FILENAME)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise PluginInstallError("?", "validate_archive", "zip 缺少 manifest.json") from exc
    try:
        return load_manifest_from_dict(json.loads(raw.decode("utf-8")))
    except (json.JSONDecodeError, ValueError) as exc:
        raise PluginInstallError("?", "validate_manifest", str(exc)) from exc


def _validate_archive_meta(zip_path: Path, sha256: str | None = None) -> None:
    if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PluginInstallError(
            "?", "validate_archive", f"zip 超过大小上限 {MAX_ARCHIVE_BYTES} 字节"
        )
    if sha256:
        actual = compute_sha256(zip_path)
        if actual != sha256.lower():
            raise PluginInstallError("?", "validate_archive", "zip sha256 不匹配")


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """安全解压：拒绝绝对路径、..、符号链接；限制文件数与解压体积。"""
    dest_root = dest_dir.resolve()
    total_bytes = 0
    file_count = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            file_count += 1
            if file_count > MAX_ARCHIVE_FILES:
                raise PluginInstallError(
                    "?", "extract", f"zip 文件数超过上限 {MAX_ARCHIVE_FILES}"
                )
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or os.path.isabs(name):
                raise PluginInstallError("?", "extract", f"zip 包含非法路径: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PluginInstallError("?", "extract", f"zip 不允许符号链接: {name}")
            total_bytes += info.file_size
            if total_bytes > MAX_UNPACKED_BYTES:
                raise PluginInstallError(
                    "?", "extract", f"zip 解压体积超过上限 {MAX_UNPACKED_BYTES} 字节"
                )
            target = (dest_root / name).resolve()
            if dest_root not in target.parents and target != dest_root:
                raise PluginInstallError("?", "extract", f"zip 包含越界路径: {name}")
            archive.extract(info, dest_root)


def _verify_manifest_files(manifest: PluginManifest, plugin_dir: Path) -> None:
    for file_name, digest in manifest.files.items():
        file_path = plugin_dir / file_name
        if not file_path.is_file():
            raise PluginInstallError(
                manifest.plugin_id, "verify_files", f"manifest.files 声明缺失: {file_name}"
            )
        if compute_sha256(file_path) != digest.lower():
            raise PluginInstallError(
                manifest.plugin_id, "verify_files", f"文件哈希不匹配: {file_name}"
            )


def _check_requires_python(manifest: PluginManifest) -> None:
    if not manifest.requires_python:
        return
    from packaging.specifiers import SpecifierSet

    specifier = SpecifierSet(manifest.requires_python)
    import platform

    if not specifier.contains(platform.python_version()):
        raise PluginInstallError(
            manifest.plugin_id,
            "validate_python",
            f"插件要求 Python {manifest.requires_python}，宿主为 {platform.python_version()}",
        )


def _ensure_newer_version(
    plugin_id: str,
    current_version: str,
    new_version: str,
) -> None:
    """升级必须提供严格更高的版本；无法按 PEP 440 解析时退化为字符串比较。"""
    from packaging.version import InvalidVersion, Version

    try:
        newer = Version(new_version) > Version(current_version)
    except InvalidVersion:
        newer = new_version > current_version
    if not newer:
        raise PluginInstallError(
            plugin_id,
            "validate_version",
            f"新版本必须高于当前版本: current={current_version} new={new_version}",
        )


class PluginInstaller:
    """文件系统层面的安装编排；配置写入由 PluginManager 负责。"""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)

    # ---- 路径辅助 ----

    def _plugin_dir(self, plugin_id: str) -> Path:
        return self.root_dir / plugin_id

    def _staging_dir(self, plugin_id: str, version: str) -> Path:
        return self.root_dir / ".staging" / f"{plugin_id}-{version}"

    def _previous_dir(self, plugin_id: str) -> Path:
        return self.root_dir / ".previous" / plugin_id

    def _trash_dir(self, plugin_id: str) -> Path:
        return self.root_dir / ".trash" / plugin_id

    # ---- 安装 ----

    def install(
        self,
        zip_path: Path,
        *,
        sha256: str | None = None,
        plugin_settings: Plugins | None = None,
    ) -> InstallResult:
        zip_path = Path(zip_path)
        _validate_archive_meta(zip_path, sha256=sha256)
        manifest = _read_manifest_from_zip(zip_path)
        plugin_id = manifest.plugin_id
        if self._plugin_dir(plugin_id).exists():
            raise PluginInstallError(
                plugin_id, "publish", "插件已安装，请使用 update 升级"
            )
        # 全新安装不继承历史快照，避免卸载残留的旧版本被自愈逻辑误恢复。
        previous_dir = self._previous_dir(plugin_id)
        if previous_dir.exists():
            shutil.rmtree(previous_dir, ignore_errors=True)
        return self._stage_and_publish(
            zip_path,
            manifest,
            action="install",
            plugin_settings=plugin_settings,
        )

    def update(
        self,
        plugin_id: str,
        zip_path: Path,
        *,
        sha256: str | None = None,
        plugin_settings: Plugins | None = None,
    ) -> InstallResult:
        zip_path = Path(zip_path)
        _validate_archive_meta(zip_path, sha256=sha256)
        manifest = _read_manifest_from_zip(zip_path)
        if manifest.plugin_id != plugin_id:
            raise PluginInstallError(
                plugin_id, "validate_manifest", "包内 plugin_id 与目标不一致"
            )
        if not self._plugin_dir(plugin_id).exists():
            raise PluginInstallError(plugin_id, "publish", "插件未安装，请使用 install")
        current_version = load_manifest_from_file(self._plugin_dir(plugin_id)).version
        _ensure_newer_version(plugin_id, current_version, manifest.version)
        return self._stage_and_publish(
            zip_path,
            manifest,
            action="update",
            plugin_settings=plugin_settings,
        )

    def _stage_and_publish(
        self,
        zip_path: Path,
        manifest: PluginManifest,
        *,
        action: str,
        plugin_settings: Plugins | None = None,
    ) -> InstallResult:
        plugin_id = manifest.plugin_id
        staging = self._staging_dir(plugin_id, manifest.version)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            safe_extract_zip(zip_path, staging)
            _verify_manifest_files(manifest, staging)
            _check_requires_python(manifest)
            if not (staging / "__init__.py").is_file():
                raise PluginInstallError(
                    plugin_id, "validate_package", "插件包根缺少 __init__.py"
                )
            # 依赖安装到暂存目录；发布时随整个目录原子切换。
            install_plugin_dependencies(
                staging,
                manifest,
                root_dir=self.root_dir,
            )
            if plugin_settings is not None:
                # 文档承诺的试加载：发布前 import + register + 契约/白名单校验，
                # 坏插件在安装期就被拒绝，而不是留到下次启动才报错。
                from src.plugins.loader import PluginLoadError, _dry_run_plugin_dir

                try:
                    _dry_run_plugin_dir(
                        plugin_id=plugin_id,
                        plugin_dir=staging,
                        root_dir=self.root_dir,
                        plugin_settings=plugin_settings,
                    )
                except PluginLoadError as exc:
                    raise PluginInstallError(
                        plugin_id,
                        exc.stage,
                        str(exc),
                    ) from exc
            self._publish(staging, plugin_id)
        except PluginInstallError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise PluginInstallError(plugin_id, "install", str(exc)) from exc
        return InstallResult(
            plugin_id=plugin_id,
            version=manifest.version,
            action=action,
        )

    def _publish(self, staging: Path, plugin_id: str) -> None:
        """发布暂存目录为正式插件目录；升级时保留 data/，旧版本进单一 .previous/ 快照。

        data/ 是宿主托管的运行数据目录：包内自带 data/ 一律丢弃，避免插件代码绕过
        import 白名单扫描或污染运行数据。
        """
        final_dir = self._plugin_dir(plugin_id)
        previous_dir = self._previous_dir(plugin_id)
        old_previous_tmp: Path | None = None
        packaged_data = staging / "data"
        if packaged_data.exists():
            shutil.rmtree(packaged_data, ignore_errors=True)
        try:
            if final_dir.exists():
                # 先腾出快照位：旧快照挪到暂存，保证 final -> previous 原子移动可执行。
                previous_dir.parent.mkdir(parents=True, exist_ok=True)
                if previous_dir.exists():
                    old_previous_tmp = (
                        self.root_dir
                        / ".staging"
                        / f"old-previous-{plugin_id}-{int(time.time())}"
                    )
                    os.replace(previous_dir, old_previous_tmp)
                # 旧版本连同 data 一起原子进入快照；随后 data 再随新目录落地，
                # 让发布中断时 loader 能从快照恢复完整旧版本。
                os.replace(final_dir, previous_dir)
                data_dir = previous_dir / "data"
                if data_dir.exists():
                    shutil.move(str(data_dir), packaged_data)
            os.replace(staging, final_dir)
            (final_dir / "data").mkdir(parents=True, exist_ok=True)
        except Exception:
            # 发布中断：优先恢复旧版本与运行数据，避免留下半成品。
            if not final_dir.exists() and previous_dir.is_dir():
                os.replace(previous_dir, final_dir)
            if packaged_data.exists() and not (final_dir / "data").exists():
                shutil.move(str(packaged_data), str(final_dir / "data"))
            raise
        finally:
            if old_previous_tmp is not None and old_previous_tmp.exists():
                shutil.rmtree(old_previous_tmp, ignore_errors=True)

    def recover_interrupted_publish(self, plugin_id: str) -> bool:
        """发布中断自愈：正式目录缺失但存在 .previous 快照时，恢复旧版本。

        正常状态（正式目录存在）不做任何事；返回是否执行了恢复。
        """
        final_dir = self._plugin_dir(plugin_id)
        previous_dir = self._previous_dir(plugin_id)
        if final_dir.exists() or not previous_dir.is_dir():
            return False
        os.replace(previous_dir, final_dir)
        # 数据可能已先挪进暂存目录（发布中断窗口），一并恢复。
        for staging in sorted(self.root_dir.glob(f".staging/{plugin_id}-*")):
            data_dir = staging / "data"
            if data_dir.is_dir() and not (final_dir / "data").exists():
                shutil.move(str(data_dir), str(final_dir / "data"))
                break
        return True

    # ---- 回滚 / 卸载 ----

    def rollback(self, plugin_id: str) -> InstallResult:
        """用 .previous/ 单一快照恢复上一个版本；回滚后快照即消费。

        当前版本先挪到 .staging 暂存，成功后再清理；中断时由恢复逻辑回填。
        """
        final_dir = self._plugin_dir(plugin_id)
        previous_dir = self._previous_dir(plugin_id)
        if not final_dir.exists() or not previous_dir.is_dir():
            raise PluginInstallError(plugin_id, "rollback", "没有可回滚的历史版本")
        data_temp = self.root_dir / ".staging" / f"rollback-data-{plugin_id}-{int(time.time())}"
        current_tmp = self.root_dir / ".staging" / f"rollback-current-{plugin_id}-{int(time.time())}"
        try:
            data_dir = final_dir / "data"
            if data_dir.exists():
                shutil.move(str(data_dir), str(data_temp))
            os.replace(final_dir, current_tmp)
            os.replace(previous_dir, final_dir)
            if data_temp.exists():
                shutil.move(str(data_temp), str(final_dir / "data"))
            shutil.rmtree(current_tmp, ignore_errors=True)
        except Exception as exc:
            # 尽量恢复现场：正式目录缺失时优先回填快照，其次暂存中的当前版本。
            if not final_dir.exists():
                if previous_dir.is_dir():
                    os.replace(previous_dir, final_dir)
                elif current_tmp.exists():
                    os.replace(current_tmp, final_dir)
            if data_temp.exists() and not (final_dir / "data").exists():
                shutil.move(str(data_temp), str(final_dir / "data"))
            shutil.rmtree(current_tmp, ignore_errors=True)
            raise PluginInstallError(plugin_id, "rollback", str(exc)) from exc
        try:
            version = load_manifest_from_file(final_dir).version
        except ValueError:
            version = ""
        return InstallResult(
            plugin_id=plugin_id,
            version=version,
            action="rollback",
        )

    def uninstall(self, plugin_id: str, *, purge_data: bool = False) -> InstallResult:
        final_dir = self._plugin_dir(plugin_id)
        if not final_dir.exists():
            raise PluginInstallError(plugin_id, "uninstall", "插件未安装")
        # 卸载即消费历史快照，避免重新启用时自愈逻辑误恢复已卸载版本。
        previous_dir = self._previous_dir(plugin_id)
        if previous_dir.exists():
            shutil.rmtree(previous_dir, ignore_errors=True)
        if purge_data:
            shutil.rmtree(final_dir, ignore_errors=True)
            # purge 语义 = 彻底删除该插件的所有本地痕迹（任务历史保留在 DB）。
            trash_dir = self._trash_dir(plugin_id)
            if trash_dir.exists():
                shutil.rmtree(trash_dir, ignore_errors=True)
        else:
            trash_dir = self._trash_dir(plugin_id)
            trash_dir.mkdir(parents=True, exist_ok=True)
            os.replace(final_dir, trash_dir / f"uninstalled-{int(time.time())}")
        return InstallResult(
            plugin_id=plugin_id,
            version="",
            action="uninstall",
            pending_restart=("api", "aps"),
        )
