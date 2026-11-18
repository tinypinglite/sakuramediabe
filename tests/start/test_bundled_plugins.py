from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from src.plugins.bundled import (
    install_bundled_plugins,
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


def test_bundled_plugin_order_and_minimal_set():
    module = _load_fetch_bundled_plugins_module()

    assert [spec.plugin_id for spec in module.BUNDLED_PLUGINS] == [
        "sakuramedia_javbus_metadata",
        "sakuramedia_avmoo_metadata",
        "sakuramedia_local_provider",
        "sakuramedia_javdb_ranking",
        "sakuramedia_more_rank_movies",
    ]


def test_javdb_ranking_plugin_is_bundled():
    module = _load_fetch_bundled_plugins_module()

    plugin_ids = [spec.plugin_id for spec in module.BUNDLED_PLUGINS]

    assert plugin_ids.count("sakuramedia_javdb_ranking") == 1


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


def _market_entry(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    host_api_version: int = 18,
    sha256: str | None = None,
    download_url: str | None = None,
) -> dict:
    if sha256 is None:
        sha256 = "a" * 64
    if download_url is None:
        download_url = (
            f"https://github.com/tinypinglite/sakuramedia-plugin-market/releases/"
            f"download/{plugin_id}-v{version}/{plugin_id}-{version}.zip"
        )
    return {
        "plugin_id": plugin_id,
        "official": True,
        "latest": {
            "version": version,
            "host_api_version": host_api_version,
            "download_url": download_url,
            "sha256": sha256,
            "published_at": "2026-11-17T00:00:00Z",
        },
    }


def _market_index(entries: list[dict]) -> dict:
    return {"schema_version": 1, "plugins": entries}


def _http_get(payloads: dict[str, object]):
    def http_get(url: str) -> bytes:
        payload = payloads[url]
        if isinstance(payload, bytes):
            return payload
        return json.dumps(payload).encode("utf-8")

    return http_get


def _plugin_zip(plugin_id: str, version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__init__.py", "")
        archive.writestr(
            "manifest.json",
            json.dumps({"plugin_id": plugin_id, "version": version}),
        )
    return buffer.getvalue()


def test_bundled_plugin_ids_are_unique():
    module = _load_fetch_bundled_plugins_module()

    plugin_ids = [spec.plugin_id for spec in module.BUNDLED_PLUGINS]

    assert len(plugin_ids) == 5
    assert len(set(plugin_ids)) == 5


def test_resolve_plugins_reads_market_index(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    entry = _market_entry("demo_plugin", version="1.2.3", sha256="b" * 64)
    http_get = _http_get(
        {module.MARKET_INDEX_URL: _market_index([entry])}
    )

    resolved = module.resolve_plugins(http_get=http_get)

    assert [(item.plugin_id, item.version, item.tag) for item in resolved] == [
        ("demo_plugin", "1.2.3", "demo_plugin-v1.2.3")
    ]
    assert resolved[0].asset_name == "demo_plugin-1.2.3.zip"
    assert resolved[0].checksum_sha256 == "b" * 64
    assert resolved[0].checksum_url is None


def test_resolve_plugins_fetches_market_index_once(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module,
        "BUNDLED_PLUGINS",
        (
            module.BundledPluginSpec("demo_a"),
            module.BundledPluginSpec("demo_b"),
        ),
    )
    calls: list[str] = []

    def http_get(url: str) -> bytes:
        calls.append(url)
        return json.dumps(
            _market_index(
                [
                    _market_entry("demo_a", version="1.0.0"),
                    _market_entry("demo_b", version="2.0.0"),
                ]
            )
        ).encode("utf-8")

    resolved = module.resolve_plugins(http_get=http_get)

    assert [item.version for item in resolved] == ["1.0.0", "2.0.0"]
    assert calls == [module.MARKET_INDEX_URL]


def test_resolve_plugins_rejects_missing_market_plugin(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    http_get = _http_get({module.MARKET_INDEX_URL: _market_index([])})

    with pytest.raises(module.BundledPluginError, match="market_index_missing_plugin"):
        module.resolve_plugins(http_get=http_get)


def test_resolve_plugins_rejects_incompatible_host_api(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    entry = _market_entry("demo_plugin", host_api_version=17)
    http_get = _http_get(
        {module.MARKET_INDEX_URL: _market_index([entry])}
    )

    with pytest.raises(module.BundledPluginError, match="market_index_incompatible"):
        module.resolve_plugins(http_get=http_get)


def test_resolve_plugins_rejects_invalid_sha256(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    entry = _market_entry("demo_plugin", sha256="not-a-sha256")
    http_get = _http_get(
        {module.MARKET_INDEX_URL: _market_index([entry])}
    )

    with pytest.raises(module.BundledPluginError, match="market_index_invalid"):
        module.resolve_plugins(http_get=http_get)


def test_pinned_plugin_uses_market_release(monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    tag = "demo_plugin-v0.9.0"
    release = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "demo_plugin-0.9.0.zip",
                "browser_download_url": "https://example.test/demo_plugin-0.9.0.zip",
            },
            {
                "name": "demo_plugin-0.9.0.zip.sha256",
                "browser_download_url": (
                    "https://example.test/demo_plugin-0.9.0.zip.sha256"
                ),
            },
        ],
    }
    release_url = (
        f"{module.GITHUB_API_ROOT}/repos/{module.MARKET_REPO}/releases/tags/{tag}"
    )
    http_get = _http_get({release_url: release})
    pins = module.parse_pins(["demo_plugin=v0.9.0"])

    resolved = module.resolve_plugins(http_get=http_get, pins=pins)

    assert resolved[0].tag == tag
    assert resolved[0].version == "0.9.0"
    assert resolved[0].checksum_url == (
        "https://example.test/demo_plugin-0.9.0.zip.sha256"
    )
    assert resolved[0].checksum_sha256 is None


def test_fetch_plugins_uses_market_sha256(tmp_path, monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    archive = _plugin_zip("demo_plugin", "1.0.0")
    digest = hashlib.sha256(archive).hexdigest()
    asset_url = "https://example.test/demo_plugin-1.0.0.zip"
    entry = _market_entry(
        "demo_plugin",
        version="1.0.0",
        sha256=digest,
        download_url=asset_url,
    )
    http_get = _http_get(
        {
            module.MARKET_INDEX_URL: _market_index([entry]),
            asset_url: archive,
        }
    )

    resolved, index_path, build_info_path = module.fetch_plugins(
        output_dir=tmp_path,
        http_get=http_get,
    )

    assert resolved[0].version == "1.0.0"
    assert (tmp_path / "demo_plugin-1.0.0.zip").read_bytes() == archive
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["plugins"] == [
        {
            "filename": "demo_plugin-1.0.0.zip",
            "plugin_id": "demo_plugin",
            "sha256": digest,
            "version": "1.0.0",
        }
    ]
    build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
    assert build_info["plugins"][0]["repo"] == module.MARKET_REPO


def test_fetch_plugins_rejects_market_checksum_mismatch(tmp_path, monkeypatch):
    module = _load_fetch_bundled_plugins_module()
    monkeypatch.setattr(
        module, "BUNDLED_PLUGINS", (module.BundledPluginSpec("demo_plugin"),)
    )
    archive = _plugin_zip("demo_plugin", "1.0.0")
    asset_url = "https://example.test/demo_plugin-1.0.0.zip"
    entry = _market_entry(
        "demo_plugin",
        version="1.0.0",
        sha256="c" * 64,
        download_url=asset_url,
    )
    http_get = _http_get(
        {
            module.MARKET_INDEX_URL: _market_index([entry]),
            asset_url: archive,
        }
    )

    with pytest.raises(module.BundledPluginError, match="archive_checksum_mismatch"):
        module.fetch_plugins(output_dir=tmp_path, http_get=http_get)
