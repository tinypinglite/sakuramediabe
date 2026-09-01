from types import SimpleNamespace

from src.plugins.provider_protocol import BrowseEntry, BrowsePage
from src.schema.transfers.media_import import ImportBrowseRequest
from src.service.transfers.imports import provider_browse_service
from src.service.transfers.imports.provider_browse_service import ProviderBrowseService


def test_browse_uses_host_video_extension_whitelist(monkeypatch) -> None:
    library = SimpleNamespace(
        id=1,
        provider_key="legacy_provider",
        provider_config={},
        account_key=None,
    )
    page = BrowsePage(
        entries=(
            BrowseEntry({}, "video.mp4", "file", 1, None, False),
            BrowseEntry({}, "disc.iso", "file", 1, None, True),
        ),
        next_cursor=None,
    )
    storage = SimpleNamespace(browse=lambda **_kwargs: page)
    monkeypatch.setattr(provider_browse_service, "require_library", lambda _id: library)
    monkeypatch.setattr(
        provider_browse_service.MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _library: storage,
    )

    result = ProviderBrowseService.browse(ImportBrowseRequest(library_id=library.id))

    assert [entry.is_video for entry in result.entries] == [True, False]
