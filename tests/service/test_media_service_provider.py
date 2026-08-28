from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback import media_service
from src.service.playback.media_service import MediaService


def test_delete_media_cleans_host_record_when_provider_reports_missing(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="delete-provider-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="DELETE-001", javdb_id="delete-1", title="delete")
    media = Media.create(movie=movie, library=library, file_name="delete.mp4")

    class Storage:
        def delete_media(self, *, media):
            raise ProviderOperationError(
                provider_key="demo",
                operation="delete_media",
                code="source_not_found",
                safe_message="source missing",
                retryable=False,
            )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    monkeypatch.setattr(
        media_service,
        "get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda _self, _media_id: None})(),
    )

    MediaService.delete_media(media.id)

    assert Media.get_or_none(Media.id == media.id) is None
