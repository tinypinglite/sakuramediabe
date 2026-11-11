from __future__ import annotations

import hashlib
import json

from src.plugins.bundled import (
    DEFAULT_BINDINGS_MARKER_FILENAME,
    install_bundled_plugins,
    migrate_plugin_defaults,
    read_bundled_index,
)


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


def test_plugin_defaults_migrate_existing_install_once(monkeypatch, tmp_path):
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
        "sakuramedia_javdb_metadata",
        "sakuramedia_javbus_metadata",
        "other_plugin",
    ]
    assert migrated.stats_source == "sakuramedia_javdb_metadata"
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
