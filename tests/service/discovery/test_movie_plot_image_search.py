from src.model import Image, Movie, MoviePlotImage
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
