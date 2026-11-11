from __future__ import annotations

import hashlib
import json

from src.plugins.bundled import install_bundled_plugins, read_bundled_index


class _FakePluginManager:
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.install_calls: list[tuple[object, str, bool]] = []

    def install_zip(self, archive_path, *, sha256, enable):
        self.install_calls.append((archive_path, sha256, enable))
        return {"plugin_id": "demo_plugin"}

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


def test_existing_plugin_is_only_skipped(tmp_path):
    bundle_dir = tmp_path / "bundled"
    _write_index(bundle_dir)
    plugin_root = tmp_path / "plugins"
    (plugin_root / "demo_plugin").mkdir(parents=True)
    manager = _FakePluginManager(plugin_root)

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.skipped_existing == ("demo_plugin",)
    assert result.installed == ()
    assert manager.install_calls == []


def test_new_plugin_is_installed_and_enabled(tmp_path):
    bundle_dir = tmp_path / "bundled"
    bundle_dir.mkdir()
    payload = b"plugin archive"
    archive_path = bundle_dir / "demo_plugin-1.0.0.zip"
    archive_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    _write_index(bundle_dir, sha256=digest)
    plugin_root = tmp_path / "plugins"
    manager = _FakePluginManager(plugin_root)

    result = install_bundled_plugins(bundle_dir, manager=manager)

    assert result.installed == ("demo_plugin",)
    assert result.skipped_existing == ()
    assert manager.install_calls == [(archive_path, digest, True)]
