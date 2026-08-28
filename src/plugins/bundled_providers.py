"""Install the two official providers bundled for the v0.5.3 bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config.config import settings
from src.plugins.dependencies import sync_plugin_dependencies
from src.plugins.installer import compute_sha256
from src.plugins.loader import check_plugin_dir
from src.plugins.manager import PluginManager

BUNDLED_PROVIDER_INDEX_NAME = "official-providers.json"
BUNDLED_PROVIDER_MARKER_NAME = ".official-providers-v053-installed.json"
OFFICIAL_PROVIDER_PLUGIN_IDS = (
    "sakuramedia_local_provider",
    "sakuramedia_115_provider",
)


class BundledProviderInstallError(RuntimeError):
    """The migration-only provider bundle is missing or invalid."""


@dataclass(frozen=True)
class BundledProviderInstallResult:
    installed: bool
    already_completed: bool


def _read_bundle_index(bundle_dir: Path) -> list[dict[str, str]]:
    index_path = bundle_dir / BUNDLED_PROVIDER_INDEX_NAME
    try:
        payload: Any = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundledProviderInstallError(
            f"bundled_provider_index_unavailable: {index_path}: {exc}"
        ) from exc
    raw_plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(raw_plugins, list):
        raise BundledProviderInstallError("bundled_provider_index_invalid: plugins")

    plugins: list[dict[str, str]] = []
    for item in raw_plugins:
        if not isinstance(item, dict):
            raise BundledProviderInstallError("bundled_provider_index_invalid: plugin")
        plugin_id = item.get("plugin_id")
        filename = item.get("filename")
        sha256 = item.get("sha256")
        if not all(
            isinstance(value, str) and value for value in (plugin_id, filename, sha256)
        ):
            raise BundledProviderInstallError("bundled_provider_index_invalid: fields")
        if Path(filename).name != filename:
            raise BundledProviderInstallError(
                f"bundled_provider_index_invalid: filename={filename}"
            )
        plugins.append({"plugin_id": plugin_id, "filename": filename, "sha256": sha256})

    if {item["plugin_id"] for item in plugins} != set(OFFICIAL_PROVIDER_PLUGIN_IDS):
        raise BundledProviderInstallError(
            "bundled_provider_index_invalid: expected exactly the local and 115 providers"
        )
    return plugins


def _write_completion_marker(marker_path: Path, plugins: list[dict[str, str]]) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = marker_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(
            {"version": 1, "plugins": plugins}, ensure_ascii=False, sort_keys=True
        ),
        encoding="utf-8",
    )
    temporary_path.replace(marker_path)


def install_bundled_provider_plugins_once(
    *,
    bundle_dir: Path = Path("/app/bundled-plugins"),
    manager: PluginManager | None = None,
) -> BundledProviderInstallResult:
    """Install and validate both providers, then write a permanent completion marker."""
    plugin_manager = manager or PluginManager()
    marker_path = plugin_manager.root_dir / BUNDLED_PROVIDER_MARKER_NAME
    if marker_path.is_file():
        return BundledProviderInstallResult(installed=False, already_completed=True)

    plugins = _read_bundle_index(Path(bundle_dir))
    for item in plugins:
        archive_path = Path(bundle_dir) / item["filename"]
        if (
            not archive_path.is_file()
            or compute_sha256(archive_path) != item["sha256"].lower()
        ):
            raise BundledProviderInstallError(
                f"bundled_provider_sha256_mismatch: plugin_id={item['plugin_id']}"
            )
    try:
        for item in plugins:
            result = plugin_manager.install_zip(
                Path(bundle_dir) / item["filename"],
                sha256=item["sha256"],
                enable=True,
            )
            if result.get("plugin_id") != item["plugin_id"]:
                raise BundledProviderInstallError(
                    "bundled_provider_id_mismatch: "
                    f"expected={item['plugin_id']} actual={result.get('plugin_id')}"
                )

        failures = sync_plugin_dependencies(
            settings.plugins,
            root_dir=plugin_manager.root_dir,
        )
        official_failures = {
            plugin_id: message
            for plugin_id, message in failures.items()
            if plugin_id in OFFICIAL_PROVIDER_PLUGIN_IDS
        }
        if official_failures:
            raise BundledProviderInstallError(
                f"bundled_provider_dependency_failed: {official_failures}"
            )
        for plugin_id in OFFICIAL_PROVIDER_PLUGIN_IDS:
            check_plugin_dir(
                plugin_dir=plugin_manager.root_dir / plugin_id,
                plugin_settings=settings.plugins,
            )
    except BundledProviderInstallError:
        raise
    except Exception as exc:
        raise BundledProviderInstallError(
            f"bundled_provider_install_failed: {exc}"
        ) from exc

    _write_completion_marker(marker_path, plugins)
    return BundledProviderInstallResult(installed=True, already_completed=False)


__all__ = [
    "BUNDLED_PROVIDER_MARKER_NAME",
    "OFFICIAL_PROVIDER_PLUGIN_IDS",
    "BundledProviderInstallError",
    "BundledProviderInstallResult",
    "install_bundled_provider_plugins_once",
]
