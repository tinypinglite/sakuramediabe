import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from src.config.config import settings
from src.plugins import HOST_API_VERSION
from src.plugins.bundled_providers import (
    BundledProviderInstallError,
    sync_bundled_provider_plugins,
)
from src.plugins.manager import PluginManager

OFFICIAL_IDS = ("sakuramedia_local_provider", "sakuramedia_115_provider")


def _write_plugin_zip(
    bundle_dir: Path, plugin_id: str, *, version: str = "1.0.0"
) -> tuple[str, str]:
    zip_name = f"{plugin_id}.zip"
    zip_path = bundle_dir / zip_name
    manifest = {
        "plugin_id": plugin_id,
        "display_name": plugin_id,
        "version": version,
        "host_api_version": HOST_API_VERSION,
        "dependencies": [],
    }
    source = (
        "from src.plugins import HOST_API_VERSION, PluginRegistration\n"
        "def register(context):\n"
        f"    return PluginRegistration(plugin_id='{plugin_id}', "
        f"display_name='{plugin_id}', version='{version}', "
        "host_api_version=HOST_API_VERSION)\n"
    )
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("__init__.py", source)
    return zip_name, hashlib.sha256(zip_path.read_bytes()).hexdigest()


def _make_bundle_dir(tmp_path: Path, *, version: str = "1.0.0") -> Path:
    bundle_dir = tmp_path / "bundled"
    bundle_dir.mkdir(parents=True)
    plugins = []
    for plugin_id in OFFICIAL_IDS:
        filename, sha256 = _write_plugin_zip(bundle_dir, plugin_id, version=version)
        plugins.append({"plugin_id": plugin_id, "filename": filename, "sha256": sha256})
    (bundle_dir / "official-providers.json").write_text(
        json.dumps({"plugins": plugins}), encoding="utf-8"
    )
    return bundle_dir


@pytest.fixture()
def isolated_plugin_settings(monkeypatch, tmp_path):
    original_enabled = list(settings.plugins.enabled)
    original_root = settings.plugins.root_dir
    root = tmp_path / "plugins"
    monkeypatch.setattr(settings.plugins, "root_dir", str(root))
    settings.plugins.enabled = []

    def fake_update_settings(new_settings):
        settings.plugins.enabled = list(new_settings.plugins.enabled)

    monkeypatch.setattr("src.plugins.manager.update_settings", fake_update_settings)
    try:
        yield root
    finally:
        settings.plugins.enabled = original_enabled
        settings.plugins.root_dir = original_root


def test_bundled_providers_install_and_update_with_newer_image_bundle(
    tmp_path, isolated_plugin_settings
):
    root = isolated_plugin_settings
    manager = PluginManager(root_dir=root)

    result = sync_bundled_provider_plugins(
        bundle_dir=_make_bundle_dir(tmp_path), manager=manager
    )

    assert result.installed is True
    assert result.updated is False
    assert set(settings.plugins.enabled) == set(OFFICIAL_IDS)
    assert all(
        (root / plugin_id / "manifest.json").is_file() for plugin_id in OFFICIAL_IDS
    )
    manager.set_enabled(OFFICIAL_IDS[0], False)
    data_file = root / OFFICIAL_IDS[0] / "data" / "state.json"
    data_file.parent.mkdir(exist_ok=True)
    data_file.write_text('{"v": 1}', encoding="utf-8")

    upgrade_bundle = _make_bundle_dir(tmp_path / "upgrade", version="1.1.0")
    second = sync_bundled_provider_plugins(bundle_dir=upgrade_bundle, manager=manager)

    assert second.installed is False
    assert second.updated is True
    assert all(
        manager.get_plugin(plugin_id)["version"] == "1.1.0"
        for plugin_id in OFFICIAL_IDS
    )
    assert manager.get_plugin(OFFICIAL_IDS[0])["enabled"] is False
    assert data_file.read_text(encoding="utf-8") == '{"v": 1}'


def test_bundled_provider_failure_does_not_install_providers(
    tmp_path, isolated_plugin_settings
):
    root = isolated_plugin_settings
    bundle_dir = _make_bundle_dir(tmp_path)
    index_path = bundle_dir / "official-providers.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["plugins"][1]["sha256"] = "0" * 64
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BundledProviderInstallError, match="sha256"):
        sync_bundled_provider_plugins(
            bundle_dir=bundle_dir,
            manager=PluginManager(root_dir=root),
        )

    assert not any((root / plugin_id).exists() for plugin_id in OFFICIAL_IDS)
