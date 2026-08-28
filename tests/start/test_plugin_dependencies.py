"""插件 manifest 依赖的启动前同步与加载隔离。"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config.config import Plugins, settings
from src.plugins import HOST_API_VERSION
from src.plugins.dependencies import (
    dependency_site_packages_dir,
    sync_plugin_dependencies,
)
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.plugins.manager import PluginManager
from src.plugins.manifest import load_manifest_from_dict


def _write_plugin(
    root: Path,
    plugin_id: str,
    *,
    dependencies: list[str] | None = None,
    init_source: str | None = None,
) -> Path:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": plugin_id,
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
                **({"dependencies": dependencies} if dependencies is not None else {}),
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        init_source
        or (
            "from src.plugins import HOST_API_VERSION, PluginRegistration\n"
            "def register(context):\n"
            f"    return PluginRegistration(plugin_id={plugin_id!r}, display_name='x', "
            "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
        ),
        encoding="utf-8",
    )
    return plugin_dir


@pytest.fixture(autouse=True)
def _clear_plugin_load_errors():
    PLUGIN_LOAD_ERRORS.clear()
    yield
    PLUGIN_LOAD_ERRORS.clear()


def test_manifest_dependencies_default_to_empty_and_validate_pep508():
    manifest = load_manifest_from_dict(
        {
            "plugin_id": "legacy_plugin",
            "display_name": "legacy",
            "version": "1.0.0",
            "host_api_version": HOST_API_VERSION,
        }
    )

    assert manifest.dependencies == []
    with pytest.raises(ValidationError, match="无效 PEP 508"):
        load_manifest_from_dict(
            {
                "plugin_id": "bad_plugin",
                "display_name": "bad",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
                "dependencies": ["--index-url https://example.invalid"],
            }
        )


def test_sync_installs_declared_dependencies_and_loader_can_import_them(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "dependency_plugin",
        dependencies=["demo-dependency>=1"],
        init_source=(
            "from demo_dependency import VALUE\n"
            "from src.plugins import HOST_API_VERSION, PluginRegistration\n"
            "def register(context):\n"
            "    return PluginRegistration(plugin_id='dependency_plugin', "
            "display_name=VALUE, version='1.0.0', "
            "host_api_version=HOST_API_VERSION, jobs=())\n"
        ),
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        target = Path(command[command.index("--target") + 1])
        (target / "demo_dependency.py").write_text("VALUE = 'dependency'\n")
        dist_info = target / "demo_dependency-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: demo-dependency\nVersion: 1.0\n"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("src.plugins.dependencies.subprocess.run", fake_run)

    assert (
        sync_plugin_dependencies(
            Plugins(enabled=["dependency_plugin"]),
            root_dir=root,
        )
        == {}
    )
    site_packages = dependency_site_packages_dir(root)
    assert commands == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--target",
            str(site_packages),
            "demo-dependency>=1",
        ]
    ]
    assert (
        sync_plugin_dependencies(
            Plugins(enabled=["dependency_plugin"]),
            root_dir=root,
        )
        == {}
    )
    assert len(commands) == 1

    try:
        loaded = load_enabled_plugins(
            Plugins(enabled=["dependency_plugin"]),
            root_dir=root,
        )
    finally:
        sys.path.remove(str(site_packages))
        sys.modules.pop("demo_dependency", None)

    assert [plugin.plugin_id for plugin in loaded] == ["dependency_plugin"]
    assert PLUGIN_LOAD_ERRORS == {}


def test_dependency_install_failure_is_persisted_and_isolates_plugin(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "broken_dependency_plugin",
        dependencies=["missing-package>=1"],
    )
    monkeypatch.setattr(
        "src.plugins.dependencies.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "ERROR: No matching distribution found\n",
        ),
    )

    failures = sync_plugin_dependencies(
        Plugins(enabled=["broken_dependency_plugin"]),
        root_dir=root,
    )

    assert failures == {
        "broken_dependency_plugin": "依赖安装失败: ERROR: No matching distribution found"
    }
    assert (
        load_enabled_plugins(
            Plugins(enabled=["broken_dependency_plugin"]),
            root_dir=root,
        )
        == ()
    )
    assert PLUGIN_LOAD_ERRORS["broken_dependency_plugin"]["stage"] == "dependencies"

    PLUGIN_LOAD_ERRORS.clear()
    monkeypatch.setattr(settings.plugins, "enabled", ["broken_dependency_plugin"])
    detail = PluginManager(root_dir=root).get_plugin("broken_dependency_plugin")
    assert detail is not None
    assert detail["load_status"] == "error"
    assert "No matching distribution" in detail["load_error"]


def test_successful_resync_clears_saved_dependency_failure(monkeypatch, tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "recovering_plugin",
        dependencies=["recovering-package>=1"],
    )
    monkeypatch.setattr(
        "src.plugins.dependencies.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "ERROR: temporary package index failure\n",
        ),
    )
    assert sync_plugin_dependencies(
        Plugins(enabled=["recovering_plugin"]),
        root_dir=root,
    )

    def fake_success(command, **_kwargs):
        target = Path(command[command.index("--target") + 1])
        dist_info = target / "recovering_package-1.0.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: recovering-package\nVersion: 1.0\n"
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("src.plugins.dependencies.subprocess.run", fake_success)
    assert (
        sync_plugin_dependencies(
            Plugins(enabled=["recovering_plugin"]),
            root_dir=root,
        )
        == {}
    )
    site_packages = dependency_site_packages_dir(root)
    try:
        loaded = load_enabled_plugins(
            Plugins(enabled=["recovering_plugin"]),
            root_dir=root,
        )
    finally:
        sys.path.remove(str(site_packages))

    assert [plugin.plugin_id for plugin in loaded] == ["recovering_plugin"]
    assert not (root / ".runtime" / "dependency-failures.json").exists()


def test_legacy_plugin_does_not_run_dependency_installer(monkeypatch, tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(root, "legacy_plugin")
    calls = []
    monkeypatch.setattr(
        "src.plugins.dependencies.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert (
        sync_plugin_dependencies(
            Plugins(enabled=["legacy_plugin"]),
            root_dir=root,
        )
        == {}
    )
    assert calls == []
    assert PluginManager(root_dir=root).pending_restart_for("legacy_plugin") == [
        "api",
        "aps",
    ]


def test_dependency_version_conflict_is_reported_as_load_failure(monkeypatch, tmp_path):
    root = tmp_path / "plugins"
    _write_plugin(
        root,
        "conflicting_plugin",
        dependencies=["packaging<1"],
    )
    monkeypatch.setattr(
        "src.plugins.dependencies.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    failures = sync_plugin_dependencies(
        Plugins(enabled=["conflicting_plugin"]),
        root_dir=root,
    )

    assert "不满足 packaging<1" in failures["conflicting_plugin"]
    assert (
        load_enabled_plugins(
            Plugins(enabled=["conflicting_plugin"]),
            root_dir=root,
        )
        == ()
    )
    assert PLUGIN_LOAD_ERRORS["conflicting_plugin"]["stage"] == "dependencies"


def test_zip_plugin_with_dependencies_is_published_before_its_import(tmp_path):
    source_root = tmp_path / "source"
    plugin_dir = _write_plugin(
        source_root,
        "zip_dependency_plugin",
        dependencies=["missing-package>=1"],
        init_source="import definitely_missing_package\n",
    )
    archive_path = tmp_path / "plugin.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(plugin_dir / "manifest.json", "manifest.json")
        archive.write(plugin_dir / "__init__.py", "__init__.py")

    manager = PluginManager(root_dir=tmp_path / "plugins")
    result = manager.install_zip(archive_path, enable=False)

    assert result == {"plugin_id": "zip_dependency_plugin", "version": "1.0.0"}
    assert manager.pending_restart_for("zip_dependency_plugin") == ["container"]
