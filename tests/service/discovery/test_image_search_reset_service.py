from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import (
    BackgroundTaskRun,
    Image,
    ImageSearchSession,
    Media,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
)
from src.service.discovery.image_search_reset_service import ImageSearchResetService
from src.service.system.task_queue_service import TaskQueueService


class _Store:
    def __init__(self):
        self.clear_count = 0

    def clear(self):
        self.clear_count += 1


def _prepare_image_search_data():
    movie = Movie.create(movie_number="RESET-001", javdb_id="reset-1", title="movie")
    library = MediaLibrary.create(
        name="reset-library", provider_key="test", provider_config={}
    )
    image = Image.create(
        origin="movies/reset.jpg",
        small="movies/reset.jpg",
        medium="movies/reset.jpg",
        large="movies/reset.jpg",
    )
    media = Media.create(movie=movie, library=library, file_name="reset.mp4")
    thumbnail = MediaThumbnail.create(
        media=media,
        image=image,
        offset=1,
        image_search_index_status=MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
    )
    plot_image = MoviePlotImage.create(
        movie=movie,
        image=image,
        image_search_index_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
    )
    ImageSearchSession.create(
        session_id="reset-session",
        query_vector=[0.1],
        expires_at=utc_now_for_db(),
    )
    return thumbnail, plot_image


def _configure_reset_dependencies(monkeypatch):
    thumbnail_store = _Store()
    plot_store = _Store()
    monkeypatch.setattr(
        "src.service.discovery.image_search_reset_service.get_embedding_client",
        lambda: SimpleNamespace(describe=lambda: SimpleNamespace(dimension=2)),
    )
    monkeypatch.setattr(
        "src.service.discovery.image_search_reset_service.get_qdrant_thumbnail_store",
        lambda: thumbnail_store,
    )
    monkeypatch.setattr(
        "src.service.discovery.image_search_reset_service.get_qdrant_plot_image_store",
        lambda: plot_store,
    )
    return thumbnail_store, plot_store


def test_reset_clears_vectors_resets_statuses_and_queues_both_indexes(test_db, monkeypatch):
    thumbnail, plot_image = _prepare_image_search_data()
    thumbnail_store, plot_store = _configure_reset_dependencies(monkeypatch)

    result = ImageSearchResetService.reset()

    assert result == {
        "sessions_deleted": 1,
        "thumbnails_reset": 1,
        "plot_images_reset": 1,
    }
    assert thumbnail_store.clear_count == 1
    assert plot_store.clear_count == 1
    assert ImageSearchSession.select().count() == 0
    assert (
        MediaThumbnail.get_by_id(thumbnail.id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
    )
    assert (
        MoviePlotImage.get_by_id(plot_image.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
    )
    assert (
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.mutex_key == "aps:image_search_index")
        .exists()
    )
    assert (
        BackgroundTaskRun.select()
        .where(BackgroundTaskRun.mutex_key == "aps:plot_image_search_index")
        .exists()
    )


def test_reset_rejects_active_indexing_without_changing_data(test_db, monkeypatch):
    thumbnail, plot_image = _prepare_image_search_data()
    thumbnail_store, plot_store = _configure_reset_dependencies(monkeypatch)
    TaskQueueService.enqueue(task_key="image_search_index", trigger_type="manual")

    with pytest.raises(ApiError) as exc_info:
        ImageSearchResetService.reset()

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "image_search_reset_conflict"
    assert thumbnail_store.clear_count == 0
    assert plot_store.clear_count == 0
    assert ImageSearchSession.select().count() == 1
    assert (
        MediaThumbnail.get_by_id(thumbnail.id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert (
        MoviePlotImage.get_by_id(plot_image.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
