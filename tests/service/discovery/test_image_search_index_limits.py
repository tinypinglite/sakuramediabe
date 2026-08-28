from types import SimpleNamespace

from src.model import MediaThumbnail, MoviePlotImage
from src.service.discovery.image_search_index_service import ImageSearchIndexService
from src.service.discovery.movie_plot_image_search_index_service import (
    MoviePlotImageSearchIndexService,
)


class _PendingIdQuery:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.limit_value: int | None = None

    def join(self, *_args, **_kwargs):
        return self

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def __iter__(self):
        ids = self.ids[: self.limit_value]
        return iter(SimpleNamespace(id=item) for item in ids)


def test_thumbnail_pending_query_applies_per_run_limit(monkeypatch):
    query = _PendingIdQuery([1, 2, 3])
    monkeypatch.setattr(MediaThumbnail, "select", lambda *_args: query)

    result = ImageSearchIndexService._pending_thumbnail_ids(2)

    assert result == [1, 2]
    assert query.limit_value == 2


def test_plot_image_pending_query_applies_per_run_limit(monkeypatch):
    query = _PendingIdQuery([1, 2, 3])
    monkeypatch.setattr(MoviePlotImage, "select", lambda *_args: query)

    result = MoviePlotImageSearchIndexService._pending_plot_image_ids(2)

    assert result == [1, 2]
    assert query.limit_value == 2
