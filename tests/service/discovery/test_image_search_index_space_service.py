from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import Image, ImageSearchIndexState, Movie, MoviePlotImage
from src.service.discovery.image_search_index_space_service import (
    IMAGE_SEARCH_INDEX_REBUILD_REQUIRED_ERROR_CODE,
    INDEX_SPACE_STATE_REBUILD_REQUIRED,
    INDEX_SPACE_STATE_UNINITIALIZED,
    ImageSearchIndexRebuildRequiredError,
    ImageSearchIndexSpaceService,
)
from src.service.discovery.image_search_service import ImageSearchService
from src.service.discovery.movie_plot_image_search_service import (
    MoviePlotImageSearchService,
)


class _Embedder:
    @staticmethod
    def describe():
        return SimpleNamespace(space_id="siglip2-current")


class _Store:
    pass


def test_empty_index_is_uninitialized_and_first_index_claims_current_space(test_db):
    status = ImageSearchIndexSpaceService.get_status("siglip2-current")

    assert status.state == INDEX_SPACE_STATE_UNINITIALIZED

    ImageSearchIndexSpaceService.prepare_for_indexing("siglip2-current")

    assert ImageSearchIndexState.get_by_id(1).indexed_space_id == "siglip2-current"


@pytest.mark.parametrize(
    "service",
    [
        lambda: ImageSearchService(store=_Store(), embedder=_Embedder()),
        lambda: MoviePlotImageSearchService(store=_Store(), embedder=_Embedder()),
    ],
)
def test_search_services_block_changed_embedding_space(test_db, service):
    ImageSearchIndexState.create(id=1, indexed_space_id="siglip2-previous")

    with pytest.raises(ApiError) as exc_info:
        service().create_session_and_first_page(b"query")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == IMAGE_SEARCH_INDEX_REBUILD_REQUIRED_ERROR_CODE
    assert exc_info.value.details == {
        "reason": "space_id_changed",
        "indexed_space_id": "siglip2-previous",
        "current_space_id": "siglip2-current",
    }


def test_existing_space_state_blocks_indexing_when_service_changes(test_db):
    ImageSearchIndexState.create(id=1, indexed_space_id="siglip2-previous")

    status = ImageSearchIndexSpaceService.get_status("siglip2-current")

    assert status.state == INDEX_SPACE_STATE_REBUILD_REQUIRED
    with pytest.raises(ImageSearchIndexRebuildRequiredError):
        ImageSearchIndexSpaceService.prepare_for_indexing("siglip2-current")


def test_legacy_completed_index_without_space_state_requires_rebuild(test_db):
    movie = Movie.create(
        movie_number="SPACE-001",
        javdb_id="space-1",
        title="space",
    )
    image = Image.create(
        origin="movies/space.jpg",
        small="movies/space.jpg",
        medium="movies/space.jpg",
        large="movies/space.jpg",
    )
    MoviePlotImage.create(
        movie=movie,
        image=image,
        image_search_index_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
    )

    status = ImageSearchIndexSpaceService.get_status("siglip2-current")

    assert status.state == INDEX_SPACE_STATE_REBUILD_REQUIRED
    assert status.indexed_space_id is None
