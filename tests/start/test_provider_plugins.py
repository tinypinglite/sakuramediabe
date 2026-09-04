from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config.config import Plugins
from src.plugins import HOST_API_VERSION
from src.plugins.contracts import PluginExtension
from src.plugins.extensions.media_provider import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    validate_media_provider_extension,
)
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.plugins.provider_protocol import (
    BrowsePage,
    ClipArtifact,
    ConfigField,
    DownloadClientHandle,
    LibraryHandle,
    MediaProviderRegistry,
    PreparedLibrary,
    ProviderOperationError,
    RemoteDownloadTask,
    ThumbnailGeneration,
)


def test_media_provider_protocol_uses_host_api_v5():
    assert HOST_API_VERSION == 5


def test_provider_operation_error_rejects_unknown_code():
    with pytest.raises(ValueError, match="unsupported provider operation error code"):
        ProviderOperationError(
            provider_key="demo",
            operation="test",
            code="unknown",  # type: ignore[arg-type]
            safe_message="provider failure",
            retryable=False,
        )


class _Storage:
    def browse(self, *, parent_ref, cursor, limit):
        return BrowsePage(entries=(), next_cursor=None)

    def scan_import_source(self, *, source_ref):
        return ()

    def read_import_file(self, *, source):
        raise NotImplementedError

    def delete_import_file(self, *, receipt):
        return None

    def stage_import_file(
        self, *, source, placement, source_disposition, operation_key
    ):
        raise NotImplementedError

    def finalize_import(self, *, receipt):
        return None

    def abort_import(self, *, receipt):
        return None

    def delete_media(self, *, media):
        return None

    def compute_file_hash(self, *, media):
        return "test-file-hash"

    async def handle_playback(self, *, media, context):
        raise NotImplementedError

    def generate_thumbnails(self, *, media, workspace):
        return ThumbnailGeneration(expected_count=0, artifacts=())

    def create_clip(self, *, media, start_offset_seconds, end_offset_seconds, workspace):
        return ClipArtifact(relative_path="clip.mp4")


class _Bundle:
    provider_key = "demo"
    display_name = "Demo"
    library_config_fields = (
        ConfigField(key="cookie", label="Cookie", input="secret", required=True),
    )
    playback_deliveries = ("proxy",)
    downloads = None

    def __init__(self):
        self.calls: list[str] = []

    def prepare_library(self, *, submitted_config, previous):
        self.calls.append("prepare_library")
        return PreparedLibrary(provider_config=submitted_config, account_key=None)

    def build_storage(self, *, library):
        self.calls.append("build_storage")
        return _Storage()


class _DownloadProvider:
    def submit(self, *, submission):
        return RemoteDownloadTask(
            remote_id="1",
            name=submission.display_name,
            state="queued",
            progress=0,
            completed_source_ref=None,
        )

    def list_tasks(self):
        return ()

    def delete_task(self, *, remote_id, delete_files):
        return None


class _Downloads:
    config_fields = ()

    def prepare_client(self, *, submitted_config, library, previous):
        return submitted_config

    def test_client(self, *, submitted_config, library):
        return None

    def build(self, *, client):
        return _DownloadProvider()


def test_media_provider_validator_does_not_construct_provider():
    bundle = _Bundle()
    validated = validate_media_provider_extension(
        plugin_id="demo_plugin",
        extension=PluginExtension(
            key=MEDIA_PROVIDER_EXTENSION_KEY,
            data=bundle,
        ),
    )
    assert validated is bundle
    assert bundle.calls == []


@pytest.mark.parametrize(
    ("deliveries", "message"),
    (
        ((), "不能为空"),
        (("redirect",), "必须包含 proxy"),
        (("proxy", "proxy"), "不可重复"),
        (("proxy", "unknown"), "包含不支持的方式"),
    ),
)
def test_media_provider_validator_requires_supported_playback_deliveries(
    deliveries, message
):
    bundle = _Bundle()
    bundle.playback_deliveries = deliveries

    with pytest.raises(ValueError, match=message):
        validate_media_provider_extension(
            plugin_id="demo_plugin",
            extension=PluginExtension(key=MEDIA_PROVIDER_EXTENSION_KEY, data=bundle),
        )


def test_media_provider_registry_builds_and_unloads_storage():
    bundle = _Bundle()
    bundle.downloads = _Downloads()
    registration = type(
        "Registration",
        (),
        {
            "plugin_id": "demo_plugin",
            "extensions": (
                PluginExtension(key=MEDIA_PROVIDER_EXTENSION_KEY, data=bundle),
            ),
        },
    )()
    registry = MediaProviderRegistry()
    assert registry.replace((registration,)) == set()
    library = LibraryHandle(
        library_id=1,
        provider_key="demo",
        provider_config={"cookie": "opaque"},
        account_key=None,
    )
    assert isinstance(registry.storage_for(library=library), _Storage)
    assert bundle.calls == ["build_storage"]
    client = DownloadClientHandle(
        client_id=1,
        library=library,
        provider_config={},
    )
    assert isinstance(registry.download_for(client), _DownloadProvider)
    bundle.downloads = None
    with pytest.raises(ProviderOperationError) as error:
        registry.download_for(client)
    assert error.value.code == "unsupported"
    assert error.value.provider_key == "demo"


def test_remote_download_task_requires_ref_only_when_completed():
    with pytest.raises(ValueError):
        RemoteDownloadTask(
            remote_id="1",
            name="x",
            state="completed",
            progress=1,
            completed_source_ref=None,
        )
    with pytest.raises(ValueError):
        RemoteDownloadTask(
            remote_id="1",
            name="x",
            state="completed",
            progress=1,
            completed_source_ref={},
        )
    with pytest.raises(ValueError):
        RemoteDownloadTask(
            remote_id="1",
            name="x",
            state="queued",
            progress=0,
            completed_source_ref={"opaque": True},
        )


@pytest.mark.parametrize("remote_id", (None, "", " ", 123))
def test_remote_download_task_requires_non_empty_remote_id(remote_id):
    with pytest.raises(ValueError):
        RemoteDownloadTask(
            remote_id=remote_id,
            name="x",
            state="queued",
            progress=0,
            completed_source_ref=None,
        )


def _write_bundle_plugin(root: Path, plugin_id: str, provider_key: str) -> None:
    package = root / plugin_id
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": plugin_id,
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
            }
        ),
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f"""from src.plugins import HOST_API_VERSION, PluginContext, PluginExtension, PluginRegistration
from src.plugins.provider_protocol import ConfigField, PreparedLibrary, BrowsePage, ThumbnailGeneration, ClipArtifact

class Storage:
    def browse(self, *, parent_ref, cursor, limit): return BrowsePage(entries=(), next_cursor=None)
    def scan_import_source(self, *, source_ref): return ()
    def read_import_file(self, *, source): raise NotImplementedError
    def delete_import_file(self, *, receipt): pass
    def stage_import_file(self, *, source, placement, source_disposition, operation_key): raise NotImplementedError
    def finalize_import(self, *, receipt): pass
    def abort_import(self, *, receipt): pass
    def delete_media(self, *, media): pass
    def compute_file_hash(self, *, media): return 'test-file-hash'
    async def handle_playback(self, *, media, context): raise NotImplementedError
    def generate_thumbnails(self, *, media, workspace): return ThumbnailGeneration(expected_count=0, artifacts=())
    def create_clip(self, *, media, start_offset_seconds, end_offset_seconds, workspace): return ClipArtifact(relative_path='clip.mp4')

class Bundle:
    provider_key = {provider_key!r}
    display_name = 'Demo'
    library_config_fields = ()
    playback_deliveries = ('proxy',)
    downloads = None
    def prepare_library(self, *, submitted_config, previous): return PreparedLibrary(provider_config=submitted_config, account_key=None)
    def build_storage(self, *, library): return Storage()

def register(context):
    return PluginRegistration(
        plugin_id={plugin_id!r}, display_name='Demo', version='1.0.0',
        host_api_version=HOST_API_VERSION,
        extensions=(PluginExtension(key='media.provider', data=Bundle()),),
    )
""",
        encoding="utf-8",
    )


def test_loader_isolates_duplicate_media_provider_key(tmp_path):
    _write_bundle_plugin(tmp_path, "first", "same")
    _write_bundle_plugin(tmp_path, "second", "same")
    PLUGIN_LOAD_ERRORS.clear()
    loaded = load_enabled_plugins(
        Plugins(enabled=["first", "second"]),
        root_dir=tmp_path,
    )
    assert [registration.plugin_id for registration in loaded] == ["first"]
    assert PLUGIN_LOAD_ERRORS["second"]["stage"] == "provider_registry"
