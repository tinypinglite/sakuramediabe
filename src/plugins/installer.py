"""插件 zip 安装器：完整性校验与安全解压。

只负责「把 zip 变成可发布的插件目录」：校验 zip 大小与可选 sha256、
安全解压到插件根目录下的暂存目录。依赖托管、回滚与回收站不在此列，
发布与启停由 ``PluginManager`` 负责。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

from src.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest_from_dict,
)

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 5000


class PluginInstallError(RuntimeError):
    """zip 安装失败；stage 标识失败环节，供 API/CLI 翻译错误信息。"""

    def __init__(self, plugin_id: str, stage: str, message: str):
        self.plugin_id = plugin_id
        self.stage = stage
        super().__init__(f"插件{stage}失败 plugin_id={plugin_id}: {message}")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_archive(zip_path: Path, sha256: str | None) -> None:
    if not zip_path.is_file():
        raise PluginInstallError("?", "zip", f"zip 不存在: {zip_path}")
    if zip_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PluginInstallError(
            "?", "zip", f"zip 超过大小上限 {MAX_ARCHIVE_BYTES} 字节"
        )
    if sha256 and compute_sha256(zip_path) != sha256.lower():
        raise PluginInstallError("?", "zip", "zip sha256 不匹配")


def _read_manifest_from_zip(zip_path: Path) -> PluginManifest:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            raw = archive.read(MANIFEST_FILENAME)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise PluginInstallError(
            "?", "zip", f"zip 无法读取或缺少 {MANIFEST_FILENAME}"
        ) from exc
    try:
        return load_manifest_from_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PluginInstallError("?", "manifest", str(exc)) from exc


def _check_requires_python(manifest: PluginManifest) -> None:
    if not manifest.requires_python:
        return
    import platform

    from packaging.specifiers import SpecifierSet

    specifier = SpecifierSet(manifest.requires_python)
    if not specifier.contains(platform.python_version()):
        raise PluginInstallError(
            manifest.plugin_id,
            "python",
            f"插件要求 Python {manifest.requires_python}，"
            f"宿主为 {platform.python_version()}",
        )


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """安全解压：拒绝绝对路径、..、符号链接，并限制文件数与解压体积。"""
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
            if name.startswith("/") or os.path.isabs(name) or ".." in Path(name).parts:
                raise PluginInstallError("?", "extract", f"zip 包含非法路径: {name}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PluginInstallError("?", "extract", f"zip 不允许符号链接: {name}")
            total_bytes += info.file_size
            if total_bytes > MAX_UNPACKED_BYTES:
                raise PluginInstallError(
                    "?",
                    "extract",
                    f"zip 解压体积超过上限 {MAX_UNPACKED_BYTES} 字节",
                )
            target = (dest_root / name).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise PluginInstallError("?", "extract", f"zip 包含越界路径: {name}")
            archive.extract(info, dest_root)


class PluginInstaller:
    """zip → 暂存目录的解包器；配置写入与发布由 PluginManager 负责。"""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir)

    def unpack(
        self,
        zip_path: Path,
        *,
        sha256: str | None = None,
    ) -> tuple[PluginManifest, Path]:
        """校验 zip 并安全解压到 ``<root>/.staging/<plugin_id>/``。

        暂存目录名必须等于 plugin_id：发布前试加载的 ``check_plugin_dir``
        以目录名推导插件 ID。任何一步失败都会清掉暂存目录再抛错。
        """
        zip_path = Path(zip_path)
        _validate_archive(zip_path, sha256)
        manifest = _read_manifest_from_zip(zip_path)
        _check_requires_python(manifest)
        staging = self.root_dir / ".staging" / manifest.plugin_id
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            safe_extract_zip(zip_path, staging)
            if not (staging / "__init__.py").is_file():
                raise PluginInstallError(
                    manifest.plugin_id,
                    "package",
                    "插件包根缺少 __init__.py",
                )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest, staging
