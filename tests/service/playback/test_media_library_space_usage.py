from types import SimpleNamespace

import pytest

from src.model import MediaLibrary
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
    StorageSpaceUsage,
)
from src.service.playback import media_library_service
from src.service.playback.media_library_service import MediaLibraryService


@pytest.fixture(autouse=True)
def clear_space_usage_cache():
    media_library_service._SPACE_USAGE_CACHE.clear()
    yield
    media_library_service._SPACE_USAGE_CACHE.clear()


def _library(name="Main", account_key=None):
    return MediaLibrary.create(
        name=name,
        provider_key="demo",
        provider_config={},
        account_key=account_key,
    )


def test_storage_space_usage_reports_provider_values(test_db, monkeypatch):
    library = _library()
    usage = StorageSpaceUsage(total_bytes=1000, used_bytes=750, free_bytes=250)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: SimpleNamespace(get_space_usage=lambda: usage),
    )

    assert MediaLibraryService.storage_space_usages() == {library.id: usage}


def test_storage_space_usage_skips_provider_without_capability(test_db, monkeypatch):
    _library()
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: SimpleNamespace())

    assert MediaLibraryService.storage_space_usages() == {}


def test_storage_space_usage_skips_uninstalled_provider(test_db, monkeypatch):
    _library()

    def _raise(_handle):
        raise ProviderUnavailableError("demo")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", _raise)

    assert MediaLibraryService.storage_space_usages() == {}


def test_storage_space_usage_skips_failing_provider(test_db, monkeypatch):
    _library()

    def _raise(_handle):
        raise ProviderOperationError(
            provider_key="demo",
            operation="get_space_usage",
            code="unavailable",
            safe_message="provider failure",
            retryable=True,
        )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", _raise)

    assert MediaLibraryService.storage_space_usages() == {}


def test_storage_space_usage_caches_by_account(test_db, monkeypatch):
    first = _library(name="First", account_key="uid-1")
    second = _library(name="Second", account_key="uid-1")
    calls: list[int] = []

    def _storage_for(_handle):
        calls.append(1)
        return SimpleNamespace(
            get_space_usage=lambda: StorageSpaceUsage(total_bytes=1000, used_bytes=750, free_bytes=250)
        )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", _storage_for)

    assert set(MediaLibraryService.storage_space_usages()) == {first.id, second.id}
    assert set(MediaLibraryService.storage_space_usages()) == {first.id, second.id}
    assert len(calls) == 1
