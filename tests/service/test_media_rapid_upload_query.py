from src.model import (
    Media,
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    Movie,
)
from src.service.transfers.media_rapid_upload.query_service import (
    MediaRapidUploadQueryService,
)
from src.service.transfers.media_rapid_upload.states import (
    FAILURE_REASON_FILE_CHANGED,
    FAILURE_REASON_NOT_HIT,
    ITEM_STATE_FAILED,
    ITEM_STATE_SUCCEEDED,
    PUBLIC_STATUS_FAILED,
)


def _media() -> Media:
    library = MediaLibrary.create(name="local")
    movie = Movie.create(javdb_id="movie-1", movie_number="ABC-001", title="Movie")
    return Media.create(movie=movie, library=library, path="/tmp/ABC-001.mp4")


def _item(media: Media, *, state: str, failure_reason: str | None = None):
    target = MediaLibrary.get_by_id(media.library_id)
    batch = MediaRapidUploadBatch.create(target_library=target, total_count=1)
    return MediaRapidUploadItem.create(
        batch=batch,
        media=media,
        active_media_id=None,
        state=state,
        source_path=media.path,
        failure_reason=failure_reason,
    )


def test_latest_non_retried_item_is_the_only_public_status_source(test_db) -> None:
    media = _media()
    old = _item(
        media,
        state=ITEM_STATE_FAILED,
        failure_reason=FAILURE_REASON_NOT_HIT,
    )
    old.state = "retried"
    old.save()
    _item(
        media,
        state=ITEM_STATE_FAILED,
        failure_reason=FAILURE_REASON_FILE_CHANGED,
    )

    statuses = MediaRapidUploadQueryService.get_latest_status_by_media([media.id])

    assert statuses == {media.id: PUBLIC_STATUS_FAILED}


def test_latest_success_removes_rapid_upload_warning(test_db) -> None:
    media = _media()
    _item(
        media,
        state=ITEM_STATE_FAILED,
        failure_reason=FAILURE_REASON_FILE_CHANGED,
    )
    _item(media, state=ITEM_STATE_SUCCEEDED)

    statuses = MediaRapidUploadQueryService.get_latest_status_by_media([media.id])

    assert statuses == {}
