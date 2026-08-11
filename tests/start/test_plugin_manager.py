"""插件管理（PluginManager + plugins CLI）护栏测试，不依赖数据库。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.config.config import settings
from src.plugins.installer import PluginInstallError
from src.plugins.manager import PluginManager
from src.start.commands import main


def _make_plugin_zip(tmp_path: Path, plugin_id: str = "demo_plugin") -> Path:
    pkg = tmp_path / plugin_id
    pkg.mkdir(exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": "演示",
                "version": "1.0.0",
                "host_api_version": 1,
                "dependencies": {"requirements": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        f"    return PluginRegistration(plugin_id='{plugin_id}', display_name='演示', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n",
        encoding="utf-8",
    )
    zip_path = tmp_path / f"{plugin_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(pkg / "manifest.json", "manifest.json")
        archive.write(pkg / "__init__.py", "__init__.py")
    return zip_path


def test_manager_list_and_detail_after_install(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_zip(tmp_path), enable=False)

    plugins = manager.list_plugins()
    assert [item["plugin_id"] for item in plugins] == ["demo_plugin"]
    assert plugins[0]["enabled"] is False
    assert plugins[0]["deps_status"] == "ok"
    assert plugins[0]["load_status"] == "ok"

    detail = manager.get_plugin("demo_plugin")
    assert detail is not None
    assert detail["version"] == "1.0.0"
    assert detail["dists"] == {}
    assert detail["data_dir"].endswith("data")


def test_manager_set_enabled_persists_via_config(monkeypatch, tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_zip(tmp_path), enable=False)
    captured = {}

    def fake_update_settings(new_settings):
        captured["enabled"] = list(new_settings.plugins.enabled)
        settings.plugins.enabled = list(new_settings.plugins.enabled)

    monkeypatch.setattr("src.plugins.manager.update_settings", fake_update_settings)
    manager.set_enabled("demo_plugin", True)
    assert captured["enabled"] == ["demo_plugin"]
    manager.set_enabled("demo_plugin", False)
    assert captured["enabled"] == []


def test_deps_status_reports_missing_deps_dir(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_zip(tmp_path), enable=False)
    plugin_dir = root / "demo_plugin"
    installed_path = plugin_dir / "installed.json"
    installed = json.loads(installed_path.read_text(encoding="utf-8"))
    installed["dists"] = {"fake-dep": "1.0.0"}
    installed_path.write_text(json.dumps(installed), encoding="utf-8")
    assert manager.list_plugins()[0]["deps_status"] == "missing"


def test_reinstall_deps_forces_install_when_fresh(monkeypatch, tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_zip(tmp_path), enable=False)
    calls = []
    monkeypatch.setattr(
        "src.plugins.manager.install_plugin_dependencies",
        lambda plugin_dir, manifest, *, root_dir: calls.append(
            (plugin_dir.name, manifest.plugin_id, root_dir)
        ),
    )
    manager.reinstall_deps("demo_plugin")
    assert calls == [("demo_plugin", "demo_plugin", root)]


def test_get_plugin_handles_corrupt_manifest(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_zip(tmp_path), enable=False)
    (root / "demo_plugin" / "manifest.json").write_text("{not json", encoding="utf-8")

    detail = manager.get_plugin("demo_plugin")
    assert detail is not None
    assert detail["load_status"] == "error"
    assert detail["load_error"]
    items = manager.list_plugins()
    assert [item["plugin_id"] for item in items] == ["demo_plugin"]
    assert items[0]["load_status"] == "error"


def test_manager_install_rejects_broken_register(tmp_path):
    root = tmp_path / "root"
    pkg = tmp_path / "bad_plugin"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "bad_plugin",
                "display_name": "坏插件",
                "version": "1.0.0",
                "host_api_version": 1,
                "dependencies": {"requirements": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(pkg / "manifest.json", "manifest.json")
        archive.write(pkg / "__init__.py", "__init__.py")

    manager = PluginManager(root_dir=root)
    with pytest.raises(PluginInstallError, match="register"):
        manager.install(zip_path, enable=False)
    assert not (root / "bad_plugin").exists()


def test_plugins_cli_list_status_install(monkeypatch, tmp_path):
    root = tmp_path / "root"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    zip_path = _make_plugin_zip(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["plugins", "install", str(zip_path), "--no-enable"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    assert "demo_plugin" in result.output

    result = runner.invoke(main, ["plugins", "status", "demo_plugin"])
    assert result.exit_code == 0, result.output
    assert "demo_plugin" in result.output
    assert "1.0.0" in result.output
