from types import SimpleNamespace

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


def test_uninstalled_media_provider_defers_without_consuming_failure_attempt(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="uninstalled-provider-library",
        provider_key="missing",
        provider_config={},
    )
    movie = Movie.create(movie_number="THUMB-001", javdb_id="thumb-1", title="thumb")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="thumb.mp4",
        thumbnail_attempt_count=1,
    )

    def unavailable(_library):
        raise ProviderUnavailableError("missing")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", unavailable)
    reporter = SimpleNamespace(emit=lambda **_kwargs: None)

    result = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)
    media = Media.get_by_id(media.id)

    assert result["deferred_media"] == 1
    assert media.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT
    assert media.thumbnail_attempt_count == 1
    assert media.thumbnail_deferred_count == 1
    assert media.thumbnail_terminal_at is None


def test_retryable_provider_unavailable_defers_without_consuming_failure_attempt(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="retryable-provider-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="THUMB-002", javdb_id="thumb-2", title="thumb")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="thumb.mp4",
        thumbnail_attempt_count=1,
    )

    def unavailable(_library):
        raise ProviderOperationError(
            provider_key="demo",
            operation="generate_thumbnails",
            code="unavailable",
            safe_message="provider unavailable",
            retryable=True,
        )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", unavailable)
    reporter = SimpleNamespace(emit=lambda **_kwargs: None)

    result = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)
    media = Media.get_by_id(media.id)

    assert result["deferred_media"] == 1
    assert media.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT
    assert media.thumbnail_attempt_count == 1
    assert media.thumbnail_deferred_count == 1
