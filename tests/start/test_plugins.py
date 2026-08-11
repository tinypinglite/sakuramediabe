"""插件机制 v2 护栏测试：包格式、安装器、依赖、loader、注册表与参数化任务。"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.config.config import Plugins
from src.plugins import HOST_API_VERSION
from src.plugins.contracts import PluginRegistration
from src.plugins.installer import PluginInstaller, PluginInstallError
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import _build_job_registry
from src.start.aps import get_job_cron_setting, resolve_job_cron_expr


def _write_package(base: Path, plugin_id: str, *, version: str = "1.0.0", init_source: str = "") -> Path:
    pkg = base / plugin_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": plugin_id,
                "version": version,
                "host_api_version": HOST_API_VERSION,
                "dependencies": {"requirements": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(init_source, encoding="utf-8")
    return pkg


def _zip_package(pkg: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(pkg / "manifest.json", "manifest.json")
        archive.write(pkg / "__init__.py", "__init__.py")


def _empty_register_source() -> str:
    return (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='__PLUGIN_ID__', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )


@pytest.fixture(autouse=True)
def _clear_load_errors():
    PLUGIN_LOAD_ERRORS.clear()
    yield
    PLUGIN_LOAD_ERRORS.clear()


def test_install_publishes_and_loads_plugin_with_params_jobs(tmp_path):
    init_source = """\
from pydantic import BaseModel
from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration
from src.scheduler.contracts import JobDefinition

class FetchParams(BaseModel):
    movie_number: str

def register(context: PluginContext) -> PluginRegistration:
    def run(reporter):
        return {"ok": True}
    def fetch(reporter, params):
        return params
    return PluginRegistration(
        plugin_id="demo_plugin",
        display_name="演示插件",
        version="1.0.0",
        host_api_version=HOST_API_VERSION,
        jobs=(
            JobDefinition(
                task_key="demo_sync",
                log_name="demo-sync",
                cli_name="demo-sync",
                cli_help="演示定时任务",
                default_cron="0 5 * * *",
                service_factory=run,
            ),
            JobDefinition(
                task_key="demo_fetch",
                log_name="demo-fetch",
                cli_name="demo-fetch",
                cli_help="演示带参任务",
                manual_only=True,
                params_schema=FetchParams,
                params_handler=fetch,
            ),
        ),
    )
"""
    pkg = _write_package(tmp_path, "demo_plugin", init_source=init_source)
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)

    root = tmp_path / "root"
    result = PluginInstaller(root).install(zip_path)
    assert result.plugin_id == "demo_plugin"

    loaded = load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["demo_plugin"]
    registration = loaded[0]
    assert {job.task_key for job in registration.jobs} == {
        "demo_sync",
        "demo_fetch",
    }
    by_key = {job.task_key: job for job in registration.jobs}
    assert by_key["demo_sync"].plugin_id == "demo_plugin"
    assert by_key["demo_fetch"].manual_only is True
    assert by_key["demo_fetch"].params_schema is not None
    assert by_key["demo_fetch"].params_schema.__name__ == "FetchParams"
    assert by_key["demo_fetch"].params_schema.model_json_schema()["properties"][
        "movie_number"
    ]
    assert get_job_cron_setting(by_key["demo_fetch"]) is None
    assert resolve_job_cron_expr(by_key["demo_fetch"]) is None
    assert (root / "demo_plugin" / "installed.json").is_file()
    assert (root / "demo_plugin" / "data").is_dir()
    assert PLUGIN_LOAD_ERRORS == {}


def test_zip_path_traversal_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../evil.py", "x = 1")
    with pytest.raises(PluginInstallError):
        PluginInstaller(tmp_path / "root").install(evil)


def test_zip_symlink_rejected(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120000 << 16)  # symlink mode
        archive.writestr(info, "target")
    with pytest.raises(PluginInstallError):
        PluginInstaller(tmp_path / "root").install(evil)


def test_checksum_mismatch_rejected(tmp_path):
    pkg = _write_package(
        tmp_path,
        "demo_plugin",
        init_source=_empty_register_source().replace("__PLUGIN_ID__", "demo_plugin"),
    )
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    with pytest.raises(PluginInstallError, match="sha256"):
        PluginInstaller(tmp_path / "root").install(
            zip_path, sha256="0" * 64
        )


def test_invalid_plugin_id_rejected(tmp_path):
    pkg = _write_package(tmp_path, "bad-id", init_source="")
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "bad-id",
                "display_name": "x",
                "version": "1.0.0",
                "host_api_version": 1,
            }
        ),
        encoding="utf-8",
    )
    zip_path = tmp_path / "bad.zip"
    _zip_package(pkg, zip_path)
    with pytest.raises(PluginInstallError):
        PluginInstaller(tmp_path / "root").install(zip_path)


def test_update_preserves_data_and_rollback_restores(tmp_path):
    root = tmp_path / "root"
    source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    pkg_v1 = _write_package(tmp_path, "demo_plugin", version="1.0.0", init_source=source)
    zip_v1 = tmp_path / "demo-1.zip"
    _zip_package(pkg_v1, zip_v1)
    PluginInstaller(root).install(zip_v1)
    data_file = root / "demo_plugin" / "data" / "state.json"
    data_file.write_text('{"v": 1}', encoding="utf-8")

    source_v2 = source.replace('version="1.0.0"', 'version="2.0.0"')
    pkg_v2 = _write_package(tmp_path, "demo_plugin", version="2.0.0", init_source=source_v2)
    zip_v2 = tmp_path / "demo-2.zip"
    _zip_package(pkg_v2, zip_v2)
    PluginInstaller(root).update("demo_plugin", zip_v2)

    assert (root / "demo_plugin" / "data" / "state.json").read_text() == '{"v": 1}'
    assert (root / "demo_plugin" / "manifest.json").read_text().find("2.0.0") != -1
    assert (root / ".previous" / "demo_plugin").is_dir()

    result = PluginInstaller(root).rollback("demo_plugin")
    assert (root / "demo_plugin" / "manifest.json").read_text().find("1.0.0") != -1
    assert (root / "demo_plugin" / "data" / "state.json").read_text() == '{"v": 1}'
    # 单一快照语义：回滚后返回真实版本，快照即消费，不能再次回滚。
    assert result.version == "1.0.0"
    assert not (root / ".previous" / "demo_plugin").exists()
    with pytest.raises(PluginInstallError):
        PluginInstaller(root).rollback("demo_plugin")


def test_update_rejects_downgrade(tmp_path):
    root = tmp_path / "root"
    source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    pkg_v2 = _write_package(
        tmp_path,
        "demo_plugin",
        version="2.0.0",
        init_source=source.replace('version="1.0.0"', 'version="2.0.0"'),
    )
    zip_v2 = tmp_path / "demo-2.zip"
    _zip_package(pkg_v2, zip_v2)
    PluginInstaller(root).install(zip_v2)

    pkg_v1 = _write_package(
        tmp_path,
        "demo_plugin",
        version="1.0.0",
        init_source=source,
    )
    zip_v1 = tmp_path / "demo-1.zip"
    _zip_package(pkg_v1, zip_v1)
    with pytest.raises(PluginInstallError, match="新版本必须高于当前版本"):
        PluginInstaller(root).update("demo_plugin", zip_v1)


def test_install_discards_packaged_data_dir(tmp_path):
    root = tmp_path / "root"
    source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    pkg = _write_package(tmp_path, "demo_plugin", init_source=source)
    data_dir = pkg / "data"
    data_dir.mkdir()
    (data_dir / "sneaky.py").write_text("x = 1", encoding="utf-8")
    zip_path = tmp_path / "demo.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(pkg / "manifest.json", "manifest.json")
        archive.write(pkg / "__init__.py", "__init__.py")
        archive.write(data_dir / "sneaky.py", "data/sneaky.py")
    PluginInstaller(root).install(zip_path)
    assert (root / "demo_plugin" / "data").is_dir()
    assert not (root / "demo_plugin" / "data" / "sneaky.py").exists()


def test_loader_recovers_interrupted_publish(tmp_path):
    root = tmp_path / "root"
    source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    pkg = _write_package(tmp_path, "demo_plugin", init_source=source)
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    installer = PluginInstaller(root)
    installer.install(zip_path)
    # 模拟发布中断：正式目录已挪走，旧版本留在 .previous。
    previous = root / ".previous" / "demo_plugin"
    previous.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / "demo_plugin", previous)

    loaded = load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["demo_plugin"]
    assert (root / "demo_plugin" / "manifest.json").is_file()
    assert not previous.exists()
    assert PLUGIN_LOAD_ERRORS == {}


def test_uninstall_moves_to_trash_and_purge_deletes(tmp_path):
    root = tmp_path / "root"
    source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    pkg = _write_package(tmp_path, "demo_plugin", init_source=source)
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    installer = PluginInstaller(root)
    installer.install(zip_path)

    installer.uninstall("demo_plugin")
    assert not (root / "demo_plugin").exists()
    assert list((root / ".trash" / "demo_plugin").iterdir())

    pkg2 = _write_package(tmp_path, "demo_plugin", init_source=source)
    zip_path2 = tmp_path / "demo2.zip"
    _zip_package(pkg2, zip_path2)
    installer.install(zip_path2)
    installer.uninstall("demo_plugin", purge_data=True)
    assert not (root / "demo_plugin").exists()
    assert not (root / ".trash" / "demo_plugin").exists()


def test_loader_isolates_broken_plugin(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n"
    )
    pkg_bad = _write_package(tmp_path, "bad_plugin", init_source=bad_source)
    zip_bad = tmp_path / "bad.zip"
    _zip_package(pkg_bad, zip_bad)
    PluginInstaller(root).install(zip_bad)

    good_source = _empty_register_source().replace("__PLUGIN_ID__", "good_plugin")
    pkg_good = _write_package(tmp_path, "good_plugin", init_source=good_source)
    zip_good = tmp_path / "good.zip"
    _zip_package(pkg_good, zip_good)
    PluginInstaller(root).install(zip_good)

    loaded = load_enabled_plugins(
        Plugins(enabled=["bad_plugin", "good_plugin"]),
        root_dir=root,
    )
    assert [registration.plugin_id for registration in loaded] == ["good_plugin"]
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "register"


def test_import_boundary_rejected(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "import src.model\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='bad_plugin', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    pkg = _write_package(tmp_path, "bad_plugin", init_source=bad_source)
    zip_path = tmp_path / "bad.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)

    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "import_boundary"


def test_import_guard_rejects_host_internal_modules(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "import src.plugins.installer\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='bad_plugin', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    pkg = _write_package(tmp_path, "bad_plugin", init_source=bad_source)
    zip_path = tmp_path / "bad.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)

    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "import_boundary"


def test_loader_clears_stale_error_on_success(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n"
    )
    pkg = _write_package(tmp_path, "bad_plugin", init_source=bad_source)
    zip_path = tmp_path / "bad.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)
    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "register"

    # 修复插件后再次加载：错误应被清除，且不残留旧模块状态。
    good_source = _empty_register_source().replace("__PLUGIN_ID__", "bad_plugin")
    (root / "bad_plugin" / "__init__.py").write_text(good_source, encoding="utf-8")
    loaded = load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["bad_plugin"]
    assert PLUGIN_LOAD_ERRORS == {}


def test_registration_version_mismatch_rejected(tmp_path):
    root = tmp_path / "root"
    source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', display_name='x', "
        "version='2.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    pkg = _write_package(tmp_path, "demo_plugin", init_source=source)
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)

    load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "validate_registration"


def test_plugin_settings_are_deeply_readonly(tmp_path):
    root = tmp_path / "root"
    good_source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', display_name="
        "str(context.settings['scrape']['overlap_days']), version='1.0.0', "
        "host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    pkg = _write_package(tmp_path, "demo_plugin", init_source=good_source)
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)
    plugin_settings = Plugins(
        enabled=["demo_plugin"],
        settings={"demo_plugin": {"scrape": {"overlap_days": 7}}},
    )
    loaded = load_enabled_plugins(plugin_settings, root_dir=root)
    assert loaded[0].display_name == "7"

    # 嵌套 dict 也禁止修改：插件尝试写入应导致 register 阶段失败。
    bad_source = good_source.replace(
        "return PluginRegistration(",
        "context.settings['scrape']['overlap_days'] = 1\n    return PluginRegistration(",
    )
    (root / "demo_plugin" / "__init__.py").write_text(bad_source, encoding="utf-8")
    load_enabled_plugins(plugin_settings, root_dir=root)
    assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "register"


def _fake_pip_install(package_name: str, version: str):
    def fake(*, target, requirements, manifest, plugin_dir, timeout=600):
        dist_info = target / f"{package_name}-{version}.dist-info"
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {package_name}\nVersion: {version}\n",
            encoding="utf-8",
        )
        (dist_info / "RECORD").write_text("METADATA,,\n", encoding="utf-8")

    return fake


def test_dependencies_host_reuse(tmp_path, monkeypatch):
    root = tmp_path / "root"
    pkg = _write_package(tmp_path, "demo_plugin", init_source=_empty_register_source())
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo_plugin",
                "display_name": "x",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
                "dependencies": {"requirements": ["httpx>=0.28.1,<0.29"]},
            }
        ),
        encoding="utf-8",
    )
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)
    installed = json.loads(
        (root / "demo_plugin" / "installed.json").read_text(encoding="utf-8")
    )
    # 宿主已有 httpx：不安装副本，dists 为空。
    assert installed["dists"] == {}
    assert not (root / "demo_plugin" / "deps").exists()


def test_dependencies_install_fresh_package(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.plugins.dependencies._pip_install",
        _fake_pip_install("fake_plugin_dep", "1.2.3"),
    )
    root = tmp_path / "root"
    pkg = _write_package(tmp_path, "demo_plugin", init_source=_empty_register_source())
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo_plugin",
                "display_name": "x",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
                "dependencies": {"requirements": ["fake-plugin-dep>=1.0"]},
            }
        ),
        encoding="utf-8",
    )
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    PluginInstaller(root).install(zip_path)
    installed = json.loads(
        (root / "demo_plugin" / "installed.json").read_text(encoding="utf-8")
    )
    assert installed["dists"] == {"fake-plugin-dep": "1.2.3"}
    assert (root / "demo_plugin" / "deps" / "fake_plugin_dep-1.2.3.dist-info").is_dir()


def test_dependencies_host_version_conflict_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.plugins.dependencies._pip_install",
        _fake_pip_install("fake_plugin_dep", "2.0.0"),
    )
    monkeypatch.setattr(
        "src.plugins.dependencies.host_distributions",
        lambda: {"fake-plugin-dep": "1.0.0"},
    )
    root = tmp_path / "root"
    pkg = _write_package(tmp_path, "demo_plugin", init_source=_empty_register_source())
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo_plugin",
                "display_name": "x",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
                "dependencies": {"requirements": ["fake-plugin-dep>=2.0"]},
            }
        ),
        encoding="utf-8",
    )
    zip_path = tmp_path / "demo.zip"
    _zip_package(pkg, zip_path)
    with pytest.raises(PluginInstallError, match="版本冲突"):
        PluginInstaller(root).install(zip_path)


def test_plugin_dep_conflict_between_plugins(tmp_path, monkeypatch):
    def fake_pip(target, package, version):
        dist_info = target / f"{package}-{version}.dist-info"
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n",
            encoding="utf-8",
        )
        (dist_info / "RECORD").write_text("METADATA,,\n", encoding="utf-8")

    installs = {"a": ("shared_dep", "1.0.0"), "b": ("shared_dep", "2.0.0")}

    def fake_pip_install(*, target, requirements, manifest, plugin_dir, timeout=600):
        package, version = installs[manifest.plugin_id]
        fake_pip(target, package, version)

    monkeypatch.setattr("src.plugins.dependencies._pip_install", fake_pip_install)
    root = tmp_path / "root"
    for plugin_id in ("a", "b"):
        source = _empty_register_source().replace("__PLUGIN_ID__", plugin_id)
        pkg = _write_package(tmp_path, plugin_id, init_source=source)
        (pkg / "manifest.json").write_text(
            json.dumps(
                {
                    "plugin_id": plugin_id,
                    "display_name": plugin_id,
                    "version": "1.0.0",
                    "host_api_version": HOST_API_VERSION,
                    "dependencies": {"requirements": ["shared-dep>=1.0"]},
                }
            ),
            encoding="utf-8",
        )
        zip_path = tmp_path / f"{plugin_id}.zip"
        _zip_package(pkg, zip_path)
        PluginInstaller(root).install(zip_path)

    loaded = load_enabled_plugins(Plugins(enabled=["a", "b"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["a"]
    assert PLUGIN_LOAD_ERRORS["b"]["stage"] == "deps_conflict"


def test_job_params_validation_rules():
    with pytest.raises(ValidationError):
        JobDefinition(
            task_key="x",
            log_name="x",
            cli_name="x",
            cli_help="x",
            manual_only=True,
            default_cron="0 5 * * *",
            service_factory=lambda reporter: None,
        )
    with pytest.raises(ValidationError):
        JobDefinition(
            task_key="x",
            log_name="x",
            cli_name="x",
            cli_help="x",
            default_cron="0 5 * * *",
            service_factory=lambda reporter: None,
            params_schema=BaseModel,
        )


def test_host_api_version_range_enforced():
    from src.plugins.contracts import PluginRegistration

    with pytest.raises(ValidationError):
        PluginRegistration(
            plugin_id="x",
            display_name="x",
            version="1.0.0",
            host_api_version=HOST_API_VERSION + 1,
        )


def test_registry_skips_plugin_job_conflicting_with_queue_key():
    def run(reporter):
        return {}

    registration = PluginRegistration(
        plugin_id="bad_plugin",
        display_name="bad",
        version="1.0.0",
        jobs=(
            JobDefinition(
                task_key="media_directory_import",
                log_name="bad-job",
                cli_name="bad-job",
                cli_help="x",
                default_cron="0 5 * * *",
                service_factory=run,
            ),
        ),
    )
    jobs = _build_job_registry([], (registration,))
    assert all(job.plugin_id != "bad_plugin" for job in jobs)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "registry_conflict"


def test_registry_isolates_plugin_job_conflicting_with_builtin():
    def run(reporter):
        return {}

    builtin = JobDefinition(
        task_key="builtin_x",
        log_name="builtin-x",
        cli_name="builtin-x",
        cli_help="x",
        cron_setting="movie_heat_cron",
        service_factory=run,
    )
    plugin_job = JobDefinition(
        task_key="builtin_x",
        log_name="plugin-x",
        cli_name="plugin-x",
        cli_help="x",
        default_cron="0 5 * * *",
        service_factory=run,
    ).model_copy(update={"plugin_id": "bad_plugin"})
    registration = PluginRegistration(
        plugin_id="bad_plugin",
        display_name="bad",
        version="1.0.0",
        jobs=(plugin_job,),
    )
    jobs = _build_job_registry([builtin], (registration,))
    assert [job.task_key for job in jobs] == ["builtin_x"]
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "registry_conflict"
