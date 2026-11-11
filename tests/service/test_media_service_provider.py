from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback import media_service
from src.service.playback.media_service import MediaService


def _create_movie(movie_number: str, *, subscribed: bool) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        title=movie_number,
        is_subscribed=subscribed,
    )


def _create_media(movie: Movie, library: MediaLibrary, file_name: str) -> Media:
    return Media.create(
        movie=movie,
        library=library,
        file_name=file_name,
        valid=False,
    )


def _patch_thumbnail_store(monkeypatch) -> None:
    monkeypatch.setattr(
        media_service,
        "get_qdrant_thumbnail_store",
        lambda: type("Store", (), {"delete_by_media_id": lambda _self, _media_id: None})(),
    )


def test_delete_media_cleans_host_record_when_provider_reports_missing(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="delete-provider-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="DELETE-001", title="delete")
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
    _patch_thumbnail_store(monkeypatch)

    MediaService.delete_media(media.id)

    assert Media.get_or_none(Media.id == media.id) is None


def test_delete_last_media_marks_subscription_for_replacement(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="replacement-library",
        provider_key="demo",
        provider_config={},
    )
    movie = _create_movie("REPLACE-001", subscribed=True)
    Movie.update(
        subscription_search_state="exhausted",
        subscription_search_attempt_count=3,
    ).where(Movie.id == movie.id).execute()
    media = _create_media(movie, library, "replacement.mp4")
    _patch_thumbnail_store(monkeypatch)

    MediaService.delete_media(media.id)

    stored = Movie.get_by_id(movie.id)
    assert stored.media_replacement_pending is True
    assert stored.subscription_search_state == "pending"
    assert stored.subscription_search_attempt_count == 0


def test_delete_media_only_marks_replacement_after_last_copy(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="multi-replacement-library",
        provider_key="demo",
        provider_config={},
    )
    movie = _create_movie("REPLACE-002", subscribed=True)
    first = _create_media(movie, library, "first.mp4")
    second = _create_media(movie, library, "second.mp4")
    _patch_thumbnail_store(monkeypatch)

    MediaService.delete_media(first.id)
    assert Movie.get_by_id(movie.id).media_replacement_pending is False

    MediaService.delete_media(second.id)
    assert Movie.get_by_id(movie.id).media_replacement_pending is True


def test_delete_last_media_does_not_mark_unsubscribed_movie(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="unsubscribed-replacement-library",
        provider_key="demo",
        provider_config={},
    )
    movie = _create_movie("REPLACE-003", subscribed=False)
    media = _create_media(movie, library, "unsubscribed.mp4")
    _patch_thumbnail_store(monkeypatch)

    MediaService.delete_media(media.id)

    assert Movie.get_by_id(movie.id).media_replacement_pending is False
