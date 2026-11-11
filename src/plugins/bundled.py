"""镜像内置官方插件的首次安装：只装不覆盖、失败不阻断启动。

内置包由镜像构建期写入 ``/app/bundled-plugins``（``official-plugins.json`` + 各插件 ZIP）；
本地手动构建的镜像该目录为空，安装步骤自动跳过。规则固定为：

- 只装不覆盖：``/data/plugins/<plugin_id>`` 已存在就跳过，不比较版本、不覆盖代码、不改启停；
- 失败不阻断启动：单个插件失败只落盘记录，容器继续启动；
- 索引本身缺失/损坏属于镜像产物问题，由调用方决定如何提示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.plugins.installer import compute_sha256
from src.plugins.manager import PluginManager
from src.plugins.manifest import PLUGIN_ID_PATTERN

BUNDLED_INDEX_NAME = "official-plugins.json"
BUNDLED_FAILURES_FILENAME = "bundled-plugin-failures.json"
DEFAULT_BUNDLE_DIR = Path("/app/bundled-plugins")
DEFAULT_STATS_SOURCE = "sakuramedia_javdb_metadata"
DEFAULT_REVIEW_SOURCE = "sakuramedia_javdb_metadata"
DEFAULT_METADATA_FALLBACK = "sakuramedia_javbus_metadata"
DEFAULT_BINDINGS_MARKER_FILENAME = "plugin-defaults-v1.json"
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class BundledPluginError(RuntimeError):
    """内置插件索引缺失或损坏；区别于单个插件安装失败。"""


@dataclass(frozen=True)
class BundledPluginEntry:
    plugin_id: str
    filename: str
    version: str
    sha256: str


@dataclass(frozen=True)
class BundledInstallResult:
    index_available: bool
    installed: tuple[str, ...] = ()
    skipped_existing: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.installed)


def _failures_path(root_dir: Path) -> Path:
    return Path(root_dir) / ".runtime" / BUNDLED_FAILURES_FILENAME


def _write_failures(root_dir: Path, failures: dict[str, str]) -> None:
    """把安装失败落盘，供插件管理界面/排障直接读取；全部成功时清掉旧记录。"""

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


def _parse_entry(item: Any, *, index_path: Path) -> BundledPluginEntry:
    if not isinstance(item, dict):
        raise BundledPluginError(f"bundled_index_invalid: {index_path} entry")
    plugin_id = item.get("plugin_id")
    filename = item.get("filename")
    version = item.get("version")
    sha256 = item.get("sha256")
    if not all(
        isinstance(value, str) and value
        for value in (plugin_id, filename, version, sha256)
    ):
        raise BundledPluginError(f"bundled_index_invalid: {index_path} fields")
    if Path(filename).name != filename:
        raise BundledPluginError(
            f"bundled_index_invalid: {index_path} filename={filename}"
        )
    if _SHA256_PATTERN.match(sha256) is None:
        raise BundledPluginError(
            f"bundled_index_invalid: {index_path} plugin_id={plugin_id} sha256"
        )
    if PLUGIN_ID_PATTERN.fullmatch(plugin_id) is None:
        raise BundledPluginError(
            f"bundled_index_invalid: {index_path} plugin_id={plugin_id}"
        )
    return BundledPluginEntry(
        plugin_id=plugin_id,
        filename=filename,
        version=version,
        sha256=sha256.lower(),
    )


def read_bundled_index(bundle_dir: Path) -> list[BundledPluginEntry] | None:
    """读取内置插件索引；目录里没有索引时返回 None（表示本镜像不含内置插件）。"""

    index_path = Path(bundle_dir) / BUNDLED_INDEX_NAME
    if not index_path.is_file():
        return None
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundledPluginError(
            f"bundled_index_unavailable: {index_path}: {exc}"
        ) from exc
    raw_plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(raw_plugins, list) or not raw_plugins:
        raise BundledPluginError(f"bundled_index_invalid: {index_path} plugins")
    entries = [_parse_entry(item, index_path=index_path) for item in raw_plugins]
    plugin_ids = [entry.plugin_id for entry in entries]
    if len(plugin_ids) != len(set(plugin_ids)):
        raise BundledPluginError(f"bundled_index_invalid: {index_path} duplicate")
    return entries


def _install_entry(
    entry: BundledPluginEntry,
    *,
    bundle_dir: Path,
    manager: PluginManager,
) -> None:
    archive_path = bundle_dir / entry.filename
    if not archive_path.is_file():
        raise BundledPluginError(f"bundled_archive_missing: {archive_path}")
    if compute_sha256(archive_path) != entry.sha256:
        raise BundledPluginError(
            f"bundled_archive_checksum_mismatch: plugin_id={entry.plugin_id}"
        )
    result = manager.install_zip(
        archive_path,
        sha256=entry.sha256,
        enable=True,
    )
    if result.get("plugin_id") != entry.plugin_id:
        raise BundledPluginError(
            "bundled_plugin_id_mismatch: "
            f"expected={entry.plugin_id} actual={result.get('plugin_id')}"
        )


def install_bundled_plugins(
    bundle_dir: Path = DEFAULT_BUNDLE_DIR,
    *,
    manager: PluginManager | None = None,
) -> BundledInstallResult:
    """按索引安装尚不存在的内置插件；已存在的插件目录一律跳过。"""

    plugin_manager = manager or PluginManager()
    bundle_dir = Path(bundle_dir)
    entries = read_bundled_index(bundle_dir)
    if entries is None:
        return BundledInstallResult(index_available=False)

    installed: list[str] = []
    skipped: list[str] = []
    failures: dict[str, str] = {}
    for entry in entries:
        # 只装不覆盖：目录已存在就让给用户（可能是更新的版本或本地改动）。
        if (plugin_manager.root_dir / entry.plugin_id).exists():
            skipped.append(entry.plugin_id)
            continue
        try:
            _install_entry(entry, bundle_dir=bundle_dir, manager=plugin_manager)
        except Exception as exc:  # 单个插件失败不能阻断容器启动
            failures[entry.plugin_id] = str(exc)
            continue
        installed.append(entry.plugin_id)

    _write_failures(plugin_manager.root_dir, failures)
    return BundledInstallResult(
        index_available=True,
        installed=tuple(installed),
        skipped_existing=tuple(skipped),
        failed=tuple(sorted(failures.items())),
    )


def _defaults_marker_path(root_dir: Path) -> Path:
    return Path(root_dir) / ".runtime" / DEFAULT_BINDINGS_MARKER_FILENAME


def _write_defaults_marker(root_dir: Path) -> None:
    path = _defaults_marker_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({"version": 1}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _move_plugin_before(
    plugin_ids: list[str],
    *,
    plugin_id: str,
    before_id: str,
) -> list[str]:
    if plugin_id not in plugin_ids or before_id not in plugin_ids:
        return plugin_ids
    if plugin_ids.index(plugin_id) < plugin_ids.index(before_id):
        return plugin_ids
    reordered = [item for item in plugin_ids if item != plugin_id]
    reordered.insert(reordered.index(before_id), plugin_id)
    return reordered


def migrate_plugin_defaults() -> None:
    """一次性统一 JavDB/JavBus 顺序与 JavDB 绑定；执行后写标记。"""

    from src.config.config import Settings, settings, update_settings

    current = Settings.model_validate(settings.model_dump())
    root_dir = Path(current.plugins.root_dir)
    if _defaults_marker_path(root_dir).exists():
        return
    if DEFAULT_STATS_SOURCE not in current.plugins.enabled:
        return
    if not (root_dir / DEFAULT_STATS_SOURCE).is_dir():
        return

    changed = False
    reordered = _move_plugin_before(
        list(current.plugins.enabled),
        plugin_id=DEFAULT_STATS_SOURCE,
        before_id=DEFAULT_METADATA_FALLBACK,
    )
    if reordered != current.plugins.enabled:
        current.plugins.enabled = reordered
        changed = True
    if current.plugins.stats_source != DEFAULT_STATS_SOURCE:
        current.plugins.stats_source = DEFAULT_STATS_SOURCE
        changed = True
    if current.plugins.review_source != DEFAULT_REVIEW_SOURCE:
        current.plugins.review_source = DEFAULT_REVIEW_SOURCE
        changed = True
    if changed:
        update_settings(current)
    _write_defaults_marker(root_dir)


__all__ = [
    "BUNDLED_FAILURES_FILENAME",
    "BUNDLED_INDEX_NAME",
    "DEFAULT_BINDINGS_MARKER_FILENAME",
    "DEFAULT_BUNDLE_DIR",
    "DEFAULT_METADATA_FALLBACK",
    "DEFAULT_REVIEW_SOURCE",
    "DEFAULT_STATS_SOURCE",
    "BundledInstallResult",
    "BundledPluginEntry",
    "BundledPluginError",
    "install_bundled_plugins",
    "migrate_plugin_defaults",
    "read_bundled_index",
]
