from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from src.plugins.bundled import (
    DEFAULT_BINDINGS_MARKER_FILENAME,
    install_bundled_plugins,
    migrate_plugin_defaults,
    read_bundled_index,
)


def _load_fetch_bundled_plugins_module():
    script_path = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "backend"
        / "fetch_bundled_plugins.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_fetch_bundled_plugins", script_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_module is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module
    return module


def test_bundled_metadata_plugin_order_startup_default():
    module = _load_fetch_bundled_plugins_module()

    assert [spec.plugin_id for spec in module.BUNDLED_PLUGINS[:4]] == [
        "sakuramedia_javbus_metadata",
        "sakuramedia_dmm_metadata",
        "sakuramedia_fc2_metadata",
        "sakuramedia_fc2cmadb_metadata",
    ]


class _FakePluginManager:
    def __init__(self, root_dir, *, installed_versions=None):
        self.root_dir = root_dir
        self.installed_versions = installed_versions or {}
        self.install_calls: list[tuple[object, str, bool]] = []
        self.upgrade_calls: list[tuple[str, object, str]] = []

    def get_plugin(self, plugin_id: str):
        version = self.installed_versions.get(plugin_id)
        if version is None:
            return None
        return {"plugin_id": plugin_id, "version": version}

    def install_zip(self, archive_path, *, sha256, enable):
        self.install_calls.append((archive_path, sha256, enable))
        return {"plugin_id": "demo_plugin"}

    def upgrade_zip(self, plugin_id, archive_path, *, sha256):
        self.upgrade_calls.append((plugin_id, archive_path, sha256))
        return {"plugin_id": plugin_id}

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        raise AssertionError("bundled install must not change existing enable state")


def _write_index(bundle_dir, *, sha256: str = "a" * 64, extra=None) -> None:
    entry = {
        "plugin_id": "demo_plugin",
        "filename": "demo_plugin-1.0.0.zip",
        "version": "1.0.0",
        "sha256": sha256,
    }
    if extra:
        entry.update(extra)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "official-plugins.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": [entry],
            }
        ),
        encoding="utf-8",
    )


def test_read_bundled_index_ignores_force_enable(tmp_path):
    _write_index(tmp_path, extra={"force_enable": True})

    entries = read_bundled_index(tmp_path)

    assert entries is not None
    assert entries[0].plugin_id == "demo_plugin"


def _prepare_bundle(tmp_path):
    bundle_dir = tmp_path / "bundled"
    bundle_dir.mkdir()
    payload = b"plugin archive"
    archive_path = bundle_dir / "demo_plugin-1.0.0.zip"
    archive_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    _write_index(bundle_dir, sha256=digest)
    return bundle_dir, archive_path, digest


def test_existing_directory_without_manifest_is_skipped(tmp_path):
    bundle_dir = tmp_path / "bundled"
    _write_index(bundle_dir)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(plugin_root)

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.skipped_existing == ("demo_plugin",)
    assert result.installed == ()
    assert result.upgraded == ()
    assert manager.install_calls == []


def test_new_plugin_is_installed_and_enabled(tmp_path):
    bundle_dir, archive_path, digest = _prepare_bundle(tmp_path)
    plugin_root = tmp_path / "plugins"
    manager = _FakePluginManager(plugin_root)

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ("demo_plugin",)
    assert result.upgraded == ()
    assert result.skipped_existing == ()
    assert manager.install_calls == [(archive_path, digest, True)]


def test_older_plugin_is_upgraded(tmp_path):
    bundle_dir, archive_path, digest = _prepare_bundle(tmp_path)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(
        plugin_root,
        installed_versions={"demo_plugin": "0.9.0"},
    )

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ()
    assert result.upgraded == ("demo_plugin",)
    assert result.skipped_existing == ()
    assert manager.install_calls == []
    assert manager.upgrade_calls == [("demo_plugin", archive_path, digest)]


def test_same_version_plugin_is_skipped(tmp_path):
    bundle_dir, _, _ = _prepare_bundle(tmp_path)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(
        plugin_root,
        installed_versions={"demo_plugin": "1.0.0"},
    )

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ()
    assert result.upgraded == ()
    assert result.skipped_existing == ("demo_plugin",)
    assert manager.upgrade_calls == []


def test_newer_plugin_is_not_downgraded(tmp_path):
    bundle_dir, _, _ = _prepare_bundle(tmp_path)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(
        plugin_root,
        installed_versions={"demo_plugin": "1.1.0"},
    )

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ()
    assert result.upgraded == ()
    assert result.skipped_existing == ("demo_plugin",)
    assert manager.upgrade_calls == []


def test_invalid_installed_version_is_recorded_as_failure(tmp_path):
    bundle_dir, _, _ = _prepare_bundle(tmp_path)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(
        plugin_root,
        installed_versions={"demo_plugin": "not-a-version"},
    )

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ()
    assert result.upgraded == ()
    assert result.skipped_existing == ()
    assert [plugin_id for plugin_id, _ in result.failed] == ["demo_plugin"]
    assert manager.upgrade_calls == []


def _prepare_settings(
    monkeypatch,
    tmp_path,
    *,
    enabled,
    stats_source=None,
    review_source=None,
):
    from src.config.config import settings

    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    settings.plugins.root_dir = str(tmp_path)
    settings.plugins.enabled = list(enabled)
    settings.plugins.stats_source = stats_source
    settings.plugins.review_source = review_source
    return settings


def _capture_updates(monkeypatch, settings):
    updates = []

    def fake_update_settings(new_settings):
        updates.append(new_settings.plugins.model_copy(deep=True))
        monkeypatch.setattr(settings, "plugins", new_settings.plugins)

    monkeypatch.setattr("src.config.config.update_settings", fake_update_settings)
    return updates


def test_plugin_defaults_bind_review_source_once(monkeypatch, tmp_path):
    enabled = [
        "sakuramedia_javbus_metadata",
        "sakuramedia_javdb_metadata",
        "other_plugin",
    ]
    settings = _prepare_settings(
        monkeypatch,
        tmp_path,
        enabled=enabled,
        stats_source="sakuramedia_javbus_metadata",
        review_source="other_review_source",
    )
    for plugin_id in (
        "sakuramedia_javdb_metadata",
        "sakuramedia_javbus_metadata",
    ):
        (tmp_path / plugin_id).mkdir()
    updates = _capture_updates(monkeypatch, settings)

    migrate_plugin_defaults()

    assert len(updates) == 1
    migrated = updates[0]
    assert migrated.enabled == [
        "sakuramedia_javbus_metadata",
        "sakuramedia_javdb_metadata",
        "other_plugin",
    ]
    assert migrated.stats_source == "sakuramedia_javbus_metadata"
    assert migrated.review_source == "sakuramedia_javdb_metadata"
    assert (
        tmp_path / ".runtime" / DEFAULT_BINDINGS_MARKER_FILENAME
    ).is_file()

    migrate_plugin_defaults()

    assert len(updates) == 1


def test_plugin_defaults_skip_without_javdb(monkeypatch, tmp_path):
    settings = _prepare_settings(
        monkeypatch,
        tmp_path,
        enabled=["sakuramedia_javbus_metadata"],
    )
    (tmp_path / "sakuramedia_javbus_metadata").mkdir()
    updates = _capture_updates(monkeypatch, settings)

    migrate_plugin_defaults()

    assert updates == []
    assert not (tmp_path / ".runtime" / DEFAULT_BINDINGS_MARKER_FILENAME).exists()
