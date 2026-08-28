from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.model import DownloadClient, Media, MediaLibrary, Movie
from src.plugins import HOST_API_VERSION
from src.plugins.manager import PluginManager
from src.service.system.plugin_removal_service import (
    PluginInUseError,
    PluginRemovalService,
)


def _make_media_provider_plugin(tmp_path: Path) -> Path:
    plugin_id = "media_provider_plugin"
    plugin_dir = tmp_path / plugin_id
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": "媒体提供方测试插件",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from src.plugins import HOST_API_VERSION, PluginExtension, PluginRegistration\n"
        "from src.plugins.provider_protocol import MEDIA_PROVIDER_EXTENSION_KEY\n"
        "\n"
        "class Bundle:\n"
        "    provider_key = 'test-storage'\n"
        "    display_name = '测试存储'\n"
        "    library_config_fields = ()\n"
        "    playback_deliveries = ('proxy',)\n"
        "    downloads = None\n"
        "\n"
        "    def prepare_library(self, *, submitted_config, previous):\n"
        "        raise NotImplementedError\n"
        "\n"
        "    def build_storage(self, *, library):\n"
        "        raise NotImplementedError\n"
        "\n"
        "def register(context):\n"
        "    return PluginRegistration(\n"
        "        plugin_id='media_provider_plugin',\n"
        "        display_name='媒体提供方测试插件',\n"
        "        version='1.0.0',\n"
        "        host_api_version=HOST_API_VERSION,\n"
        "        extensions=(PluginExtension(key=MEDIA_PROVIDER_EXTENSION_KEY, data=Bundle()),),\n"
        "    )\n",
        encoding="utf-8",
    )
    return plugin_dir


def test_provider_plugin_removal_preserves_data_when_unused(
    test_db, monkeypatch, tmp_path
):
    root = tmp_path / "plugins"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    manager = PluginManager(root_dir=root)
    manager.install(_make_media_provider_plugin(tmp_path), enable=False)
    data_file = root / "media_provider_plugin" / "data" / "state.json"
    data_file.parent.mkdir(parents=True)
    data_file.write_text('{"cursor": 3}', encoding="utf-8")

    PluginRemovalService.remove("media_provider_plugin")

    assert data_file.read_text(encoding="utf-8") == '{"cursor": 3}'
    assert not (root / "media_provider_plugin" / "manifest.json").exists()


def test_provider_plugin_removal_is_blocked_while_library_is_in_use(
    test_db,
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "plugins"
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: root)
    manager = PluginManager(root_dir=root)
    manager.install(_make_media_provider_plugin(tmp_path), enable=False)
    library = MediaLibrary.create(name="受保护媒体库", provider_key="test-storage")
    movie = Movie.create(
        movie_number="REMOVE-001", javdb_id="remove-001", title="remove"
    )
    Media.create(movie=movie, library=library, file_name="remove.mp4")
    DownloadClient.create(name="受保护下载器", library=library)

    with pytest.raises(PluginInUseError) as error:
        PluginRemovalService.remove("media_provider_plugin")

    assert error.value.details == {
        "plugin_id": "media_provider_plugin",
        "provider_keys": ["test-storage"],
        "library_ids": [library.id],
        "media_count": 1,
        "download_client_count": 1,
    }
    assert (root / "media_provider_plugin" / "manifest.json").is_file()
