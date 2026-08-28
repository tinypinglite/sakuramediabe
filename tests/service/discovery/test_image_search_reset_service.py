import pytest

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import (
    BackgroundTaskRun,
    Image,
    ImageSearchIndexState,
    ImageSearchSession,
    Media,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
)
from src.service.discovery.image_search_reset_service import ImageSearchResetService
from src.service.system.task_queue_service import TaskQueueService


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


def test_reset_queues_async_rebuild_without_sync_mutations(test_db):
    thumbnail, plot_image = _prepare_image_search_data()

    result = ImageSearchResetService.reset()

    assert ImageSearchSession.select().count() == 1
    assert (
        MediaThumbnail.get_by_id(thumbnail.id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert (
        MoviePlotImage.get_by_id(plot_image.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    task_run = BackgroundTaskRun.get(
        BackgroundTaskRun.mutex_key == "aps:image_search_index"
    )
    assert result == {"task_run_id": task_run.id}
    assert task_run.params == {"reset": True}
    assert BackgroundTaskRun.select().count() == 1
    assert ImageSearchIndexState.select().count() == 0


def test_reset_rejects_active_indexing_without_changing_data(test_db):
    thumbnail, plot_image = _prepare_image_search_data()
    TaskQueueService.enqueue(task_key="image_search_index", trigger_type="manual")

    with pytest.raises(ApiError) as exc_info:
        ImageSearchResetService.reset()

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "image_search_reset_conflict"
    assert ImageSearchSession.select().count() == 1
    assert (
        MediaThumbnail.get_by_id(thumbnail.id).image_search_index_status
        == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert (
        MoviePlotImage.get_by_id(plot_image.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert ImageSearchIndexState.select().count() == 0
