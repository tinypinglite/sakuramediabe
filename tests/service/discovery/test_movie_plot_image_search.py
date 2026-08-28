from types import SimpleNamespace

from src.model import Image, Movie, MoviePlotImage
from src.service.discovery.movie_plot_image_search_index_service import (
    MoviePlotImageSearchIndexService,
)
from src.service.discovery.movie_plot_image_search_service import (
    MoviePlotImageSearchService,
)
from src.service.discovery.qdrant_plot_image_store import PlotImageVectorSearchHit


def _create_plot_image(movie: Movie, origin: str) -> MoviePlotImage:
    image = Image.create(origin=origin, small=origin, medium=origin, large=origin)
    return MoviePlotImage.create(movie=movie, image=image)


class _SearchStore:
    def __init__(self, hits: list[PlotImageVectorSearchHit]) -> None:
        self.hits = hits

    def search(self, _vector, limit, offset, _movie_ids, _exclude_movie_ids):
        return self.hits[offset : offset + limit]


class _SearchEmbedder:
    def embed_images(self, _images):
        return [[0.1, 0.2]]

    def embed_texts(self, _texts):
        return [[0.1, 0.2]]


def test_plot_image_search_returns_dedicated_result_and_paginates(test_db):
    first_movie = Movie.create(
        movie_number="PLOT-001", javdb_id="plot-1", title="first"
    )
    second_movie = Movie.create(
        movie_number="PLOT-002", javdb_id="plot-2", title="second"
    )
    first = _create_plot_image(first_movie, "movies/plot-1.jpg")
    second = _create_plot_image(second_movie, "movies/plot-2.jpg")
    service = MoviePlotImageSearchService(
        store=_SearchStore(
            [
                PlotImageVectorSearchHit(
                    plot_image_id=first.id, movie_id=first_movie.id, score=0.9
                ),
                PlotImageVectorSearchHit(
                    plot_image_id=second.id, movie_id=second_movie.id, score=0.8
                ),
            ]
        ),
        embedder=_SearchEmbedder(),
    )

    first_page = service.create_session_and_first_page(b"query", page_size=1)
    second_page = service.list_results(first_page.session_id, first_page.next_cursor)

    assert [item.plot_image_id for item in first_page.items] == [first.id]
    assert first_page.items[0].movie_number == "PLOT-001"
    assert first_page.next_cursor
    assert [item.plot_image_id for item in second_page.items] == [second.id]
    assert second_page.next_cursor is None


def test_plot_image_index_marks_success(
    test_db, monkeypatch, tmp_path
):
    movie = Movie.create(movie_number="PLOT-005", javdb_id="plot-5", title="movie")
    success = _create_plot_image(movie, "movies/plot-5-success.jpg")
    failed = _create_plot_image(movie, "movies/plot-5-failed.jpg")
    image_file = tmp_path / "plot.jpg"
    image_file.write_bytes(b"image")
    monkeypatch.setattr(
        "src.service.discovery.movie_plot_image_search_index_service.resolve_image_file_path",
        lambda _origin: image_file,
    )

    class _Store:
        def __init__(self):
            self.records = []

        def ensure_table(self, vector_size):
            assert vector_size == 2

        def ensure_scalar_indices(self):
            return None

        def upsert_records(self, records):
            self.records.extend(records)

    class _Embedder:
        def describe(self):
            return SimpleNamespace(dimension=2)

        def embed_images(self, payloads):
            return [[0.2, 0.3] for _ in payloads]

    store = _Store()
    stats = MoviePlotImageSearchIndexService(
        store=store, embedder=_Embedder()
    ).index_pending_plot_images()

    assert stats == {
        "pending_plot_images": 2,
        "successful_plot_images": 2,
        "failed_plot_images": 0,
    }
    assert [record.plot_image_id for record in store.records] == [success.id, failed.id]
    assert (
        MoviePlotImage.get_by_id(success.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
    assert (
        MoviePlotImage.get_by_id(failed.id).image_search_index_status
        == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
    )
