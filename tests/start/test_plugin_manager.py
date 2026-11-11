"""插件管理（PluginManager + plugins CLI）护栏测试，不依赖数据库。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.config.config import Plugins, settings
from src.i18n import LanguageState, reset_language_state, set_language_state
from src.plugins import HOST_API_VERSION, MIN_SUPPORTED_HOST_API_VERSION
from src.plugins.installer import PluginInstallError
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.plugins.manager import PluginManager, PluginSettingsValidationError
from src.start.commands import main


@pytest.fixture(autouse=True)
def _isolate_plugin_settings(monkeypatch):
    """插件管理测试只依赖本用例写入的配置，不继承仓库 config.toml。

    否则 `plugins.enabled` 里仓库预置的插件会让 set_enabled/install 断言
    依赖测试执行顺序（单独跑与全量跑结果不一致）。
    """
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    settings.plugins.enabled = []
    settings.plugins.settings = {}


def _make_plugin_dir(
    tmp_path: Path,
    plugin_id: str = "demo_plugin",
    *,
    version: str = "1.0.0",
    broken_register: bool = False,
    release_api_url: str | None = None,
    manifest_host_api_version: int | None = None,
) -> Path:
    pkg = tmp_path / plugin_id
    pkg.mkdir(exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": "演示",
                "version": version,
                "host_api_version": (
                    manifest_host_api_version
                    if manifest_host_api_version is not None
                    else HOST_API_VERSION
                ),
                **(
                    {"release_api_url": release_api_url}
                    if release_api_url is not None
                    else {}
                ),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if broken_register:
        register_source = (
            "from src.plugins import PluginContext, PluginRegistration\n"
            "def register(context):\n"
            "    raise RuntimeError('boom')\n"
        )
    else:
        register_source = (
            "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
            "def register(context):\n"
            f"    return PluginRegistration(plugin_id='{plugin_id}', display_name='演示', "
            f"version='{version}', host_api_version=HOST_API_VERSION, jobs=())\n"
        )
    (pkg / "__init__.py").write_text(register_source, encoding="utf-8")
    return pkg


def _make_plugin_zip(
    tmp_path: Path,
    plugin_id: str = "demo_plugin",
    *,
    version: str = "1.0.0",
    broken_register: bool = False,
    manifest_host_api_version: int | None = None,
) -> Path:
    pkg = _make_plugin_dir(
        tmp_path,
        plugin_id,
        version=version,
        broken_register=broken_register,
        manifest_host_api_version=manifest_host_api_version,
    )
    zip_path = tmp_path / f"{plugin_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(pkg / "manifest.json", "manifest.json")
        archive.write(pkg / "__init__.py", "__init__.py")
    return zip_path


def test_manager_list_and_detail_after_install(tmp_path):
    root = tmp_path / "root"
    source = _make_plugin_dir(
        tmp_path,
        release_api_url="https://api.github.com/repos/example/demo/releases/latest",
    )
    manager = PluginManager(root_dir=root)
    result = manager.install(source, enable=False)

    assert result == {"plugin_id": "demo_plugin", "version": "1.0.0"}
    plugins = manager.list_plugins()
    assert [item["plugin_id"] for item in plugins] == ["demo_plugin"]
    assert plugins[0]["enabled"] is False
    assert plugins[0]["load_status"] == "ok"
    assert plugins[0]["release_api_url"].endswith("/releases/latest")

    detail = manager.get_plugin("demo_plugin")
    assert detail is not None
    assert detail["version"] == "1.0.0"
    assert detail["data_dir"].endswith("data")
    assert detail["release_api_url"].endswith("/releases/latest")


def test_manager_set_enabled_persists_via_config(monkeypatch, tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_dir(tmp_path), enable=False)
    captured = {}

    def fake_update_settings(new_settings):
        captured["enabled"] = list(new_settings.plugins.enabled)
        settings.plugins.enabled = list(new_settings.plugins.enabled)

    monkeypatch.setattr("src.plugins.manager.update_settings", fake_update_settings)
    manager.set_enabled("demo_plugin", True)
    assert captured["enabled"] == ["demo_plugin"]
    manager.set_enabled("demo_plugin", False)
    assert captured["enabled"] == []


def test_manager_plugin_settings_roundtrip_persists(monkeypatch, tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_dir(tmp_path), enable=False)
    captured = {}

    def fake_update_settings(new_settings):
        captured["settings"] = dict(new_settings.plugins.settings)
        settings.plugins.settings = dict(new_settings.plugins.settings)

    monkeypatch.setattr("src.plugins.manager.update_settings", fake_update_settings)

    assert manager.get_plugin_settings("demo_plugin") == {}

    values = {"overlap_days": 7, "tags": ["4k"]}
    result = manager.set_plugin_settings("demo_plugin", values)
    assert result == values
    assert captured["settings"]["demo_plugin"] == values
    assert manager.get_plugin_settings("demo_plugin") == values

    with pytest.raises(PluginSettingsValidationError, match="null"):
        manager.set_plugin_settings("demo_plugin", {"secret": None})
    with pytest.raises(PluginSettingsValidationError, match="null"):
        manager.set_plugin_settings(
            "demo_plugin",
            {"tags": ["4k", None]},
        )
    with pytest.raises(ValueError, match="非法插件 ID"):
        manager.set_plugin_settings("bad-id!", {})
    with pytest.raises(ValueError, match="插件未安装"):
        manager.set_plugin_settings("missing_plugin", {})
    with pytest.raises(ValueError, match="插件未安装"):
        manager.get_plugin_settings("missing_plugin")

    settings.plugins.settings = {}


def test_manager_install_replaces_code_and_preserves_data(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_dir(tmp_path, version="1.0.0"), enable=False)
    data_file = root / "demo_plugin" / "data" / "state.json"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text('{"v": 1}', encoding="utf-8")

    source_v2 = _make_plugin_dir(tmp_path, version="2.0.0")
    (source_v2 / "__init__.py").write_text(
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', display_name='v2', "
        "version='2.0.0', host_api_version=HOST_API_VERSION, jobs=())\n",
        encoding="utf-8",
    )
    manager.install(source_v2, enable=False)

    assert manager.get_plugin("demo_plugin")["version"] == "2.0.0"
    assert data_file.read_text() == '{"v": 1}'


def test_manager_install_zip_publishes_plugin(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    result = manager.install_zip(_make_plugin_zip(tmp_path), enable=False)

    assert result == {"plugin_id": "demo_plugin", "version": "1.0.0"}
    assert (root / "demo_plugin" / "manifest.json").is_file()
    assert manager.get_plugin("demo_plugin")["enabled"] is False


def test_manager_install_zip_accepts_current_manifest_on_current_host(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)

    result = manager.install_zip(
        _make_plugin_zip(
            tmp_path,
            manifest_host_api_version=MIN_SUPPORTED_HOST_API_VERSION,
        ),
        enable=False,
    )

    assert result == {"plugin_id": "demo_plugin", "version": "1.0.0"}


def test_manager_install_zip_replaces_code_and_preserves_data(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install_zip(_make_plugin_zip(tmp_path, version="1.0.0"), enable=False)
    data_file = root / "demo_plugin" / "data" / "state.json"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text('{"v": 1}', encoding="utf-8")

    manager.install_zip(_make_plugin_zip(tmp_path, version="2.0.0"), enable=False)

    assert manager.get_plugin("demo_plugin")["version"] == "2.0.0"
    assert data_file.read_text() == '{"v": 1}'


def test_manager_upgrade_zip_replaces_only_newer_same_plugin_and_preserves_data(
    tmp_path,
):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install_zip(_make_plugin_zip(tmp_path, version="1.0.0"), enable=False)
    data_file = root / "demo_plugin" / "data" / "state.json"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text('{"v": 1}', encoding="utf-8")

    result = manager.upgrade_zip(
        "demo_plugin",
        _make_plugin_zip(tmp_path, version="1.1.0"),
    )

    assert result == {"plugin_id": "demo_plugin", "version": "1.1.0"}
    assert manager.get_plugin("demo_plugin")["enabled"] is False
    assert data_file.read_text(encoding="utf-8") == '{"v": 1}'

    with pytest.raises(ValueError, match="必须高于当前版本"):
        manager.upgrade_zip(
            "demo_plugin",
            _make_plugin_zip(tmp_path, version="1.1.0"),
        )

    with pytest.raises(ValueError, match="plugin_id 不匹配"):
        manager.upgrade_zip(
            "demo_plugin",
            _make_plugin_zip(tmp_path, "other_plugin", version="2.0.0"),
        )

    assert manager.get_plugin("demo_plugin")["version"] == "1.1.0"


def test_manager_install_zip_rejects_checksum_mismatch(tmp_path):
    manager = PluginManager(root_dir=tmp_path / "root")
    with pytest.raises(PluginInstallError, match="sha256"):
        manager.install_zip(
            _make_plugin_zip(tmp_path),
            sha256="0" * 64,
            enable=False,
        )


def test_manager_install_zip_rejects_path_traversal(tmp_path):
    manifest = _make_plugin_dir(tmp_path) / "manifest.json"
    zip_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(manifest, "manifest.json")
        archive.writestr("../evil.py", "raise RuntimeError('boom')")

    manager = PluginManager(root_dir=tmp_path / "root")
    with pytest.raises(PluginInstallError, match="非法路径"):
        manager.install_zip(zip_path, enable=False)


def test_manager_install_zip_rejects_symlink_member(tmp_path):
    manifest = _make_plugin_dir(tmp_path) / "manifest.json"
    zip_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(manifest, "manifest.json")
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120000 << 16
        archive.writestr(info, "target")

    manager = PluginManager(root_dir=tmp_path / "root")
    with pytest.raises(PluginInstallError, match="符号链接"):
        manager.install_zip(zip_path, enable=False)


def test_manager_install_zip_rejects_broken_register(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    with pytest.raises(PluginInstallError, match="register"):
        manager.install_zip(
            _make_plugin_zip(tmp_path, broken_register=True),
            enable=False,
        )
    assert not (root / "demo_plugin").exists()
    assert not (root / ".staging" / "demo_plugin").exists()


def test_manager_remove_disables_and_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.plugins.manager.update_settings",
        lambda new_settings: monkeypatch.setattr(settings, "plugins", new_settings.plugins),
    )
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_dir(tmp_path), enable=True)
    data_file = root / "demo_plugin" / "data" / "state.json"
    data_file.parent.mkdir(parents=True)
    data_file.write_text('{"v": 1}', encoding="utf-8")

    manager.remove("demo_plugin")

    assert data_file.read_text(encoding="utf-8") == '{"v": 1}'
    assert not (root / "demo_plugin" / "manifest.json").exists()
    assert manager.get_plugin("demo_plugin") is None
    assert "demo_plugin" not in manager._enabled_ids()

    manager.install(_make_plugin_dir(tmp_path, version="2.0.0"), enable=False)

    assert manager.get_plugin("demo_plugin")["version"] == "2.0.0"
    assert data_file.read_text(encoding="utf-8") == '{"v": 1}'


def test_manager_install_requires_manifest(tmp_path):
    root = tmp_path / "root"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    manager = PluginManager(root_dir=root)
    with pytest.raises(ValueError, match="manifest"):
        manager.install(empty_dir, enable=False)


def test_get_plugin_handles_corrupt_manifest(tmp_path):
    root = tmp_path / "root"
    manager = PluginManager(root_dir=root)
    manager.install(_make_plugin_dir(tmp_path), enable=False)
    (root / "demo_plugin" / "manifest.json").write_text("{not json", encoding="utf-8")

    detail = manager.get_plugin("demo_plugin")
    assert detail is not None
    assert detail["load_status"] == "error"
    assert detail["load_error"]
    items = manager.list_plugins()
    assert [item["plugin_id"] for item in items] == ["demo_plugin"]
    assert items[0]["load_status"] == "error"


def test_get_plugin_strips_internal_i18n_and_localizes_display_name(tmp_path):
    source = _make_plugin_dir(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["display_name_i18n"] = {"en": "Demo Plugin", "zh-CN": "演示"}
    manifest["description"] = "演示插件"
    manifest["description_i18n"] = {"en": "Demo plugin", "zh-CN": "演示插件"}
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    manager = PluginManager(root_dir=tmp_path / "root")
    manager.install(source, enable=False)

    token = set_language_state(LanguageState(header_language="en"))
    try:
        detail = manager.get_plugin("demo_plugin")
    finally:
        reset_language_state(token)

    assert detail is not None
    assert detail["display_name"] == "Demo Plugin"
    assert detail["manifest"]["display_name"] == "Demo Plugin"
    assert detail["manifest"]["description"] == "Demo plugin"
    assert "display_name_i18n" not in detail["manifest"]
    assert "_i18n" not in json.dumps(detail["manifest"])


def test_plugins_cli_check_rejects_invalid_plugin_catalog(tmp_path):
    plugin_dir = _make_plugin_dir(tmp_path)
    i18n_dir = plugin_dir / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text(
        json.dumps({"nested.value": {"deep": "text"}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["plugins", "check", str(plugin_dir)])

    assert result.exit_code != 0
    assert "nested.value" in result.output


def test_plugins_cli_list_install_check(monkeypatch, tmp_path):
    root = tmp_path / "root"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    plugin_dir = _make_plugin_dir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["plugins", "install", str(plugin_dir), "--no-enable"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["plugins", "list"])
    assert result.exit_code == 0, result.output
    assert "demo_plugin" in result.output

    result = runner.invoke(main, ["plugins", "check", str(plugin_dir)])
    assert result.exit_code == 0, result.output
    assert "校验通过" in result.output

    zip_path = _make_plugin_zip(tmp_path, "zip_plugin")
    result = runner.invoke(
        main,
        ["plugins", "install", str(zip_path), "--no-enable"],
    )
    assert result.exit_code == 0, result.output
    assert "zip_plugin" in result.output

    bad_dir = _make_plugin_dir(tmp_path, "bad_plugin", broken_register=True)
    result = runner.invoke(main, ["plugins", "check", str(bad_dir)])
    assert result.exit_code != 0
    assert "校验失败" in result.output


def test_plugin_load_error_renders_in_request_language(tmp_path):
    _make_plugin_dir(tmp_path, broken_register=True)
    load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=tmp_path)
    try:
        assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "register"
        manager = PluginManager(root_dir=tmp_path)
        assert "插件加载失败" in manager.list_plugins()[0]["load_error"]

        token = set_language_state(LanguageState(header_language="en"))
        try:
            english_error = manager.list_plugins()[0]["load_error"]
        finally:
            reset_language_state(token)

        assert english_error.startswith("Plugin load failed")
        assert "boom" in english_error
    finally:
        PLUGIN_LOAD_ERRORS.clear()
