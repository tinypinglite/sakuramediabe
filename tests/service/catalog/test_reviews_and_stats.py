from types import SimpleNamespace

from src.config.config import settings
from src.model import Movie
from src.plugins.extensions.reviews import PluginMovieReview
from src.service.catalog.movie_review_source_service import MovieReviewSourceService
from src.service.catalog.movie_stats_service import MovieStatsService


def _install_review_sources(monkeypatch, sources):
    MovieReviewSourceService.sources = tuple(
        (plugin_id, plugin_id, {}, source)
        for plugin_id, source in sources
    )
    monkeypatch.setattr(settings.plugins, "enabled", [plugin_id for plugin_id, _ in sources])


def test_review_source_none_continues_empty_wins_and_errors_continue(monkeypatch):
    calls: list[str] = []

    class EmptySource:
        def fetch_reviews(self, *_args):
            calls.append("empty")
            return []

    class ErrorSource:
        def fetch_reviews(self, *_args):
            calls.append("error")
            raise RuntimeError("boom")

    class LaterSource:
        def fetch_reviews(self, *_args):
            calls.append("later")
            return [PluginMovieReview(id=1, content="later")]

    _install_review_sources(
        monkeypatch,
        [
            ("missing", SimpleNamespace(fetch_reviews=lambda *_args: None)),
            ("error", ErrorSource()),
            ("empty", EmptySource()),
            ("later", LaterSource()),
        ],
    )

    result = MovieReviewSourceService.fetch(
        "ABP-001", page=1, page_size=20, sort="recently"
    )

    assert result == []
    assert calls == ["error", "empty"]


def test_stats_update_writes_fields_and_recomputes_heat(test_db, monkeypatch):
    movie = Movie.create(
        movie_number="STATS-001",
        title="stats",
        score=0,
        watched_count=0,
    )
    heat_calls: list[int] = []
    monkeypatch.setattr(
        "src.service.catalog.movie_stats_service.MovieHeatService.update_single_movie_heat",
        classmethod(lambda _cls, movie_id: heat_calls.append(movie_id)),
    )

    changed = MovieStatsService.update(
        movie.id,
        {
            "score": 4.5,
            "watched_count": 12,
            "comment_count": 3,
        },
    )

    assert changed is True
    stored = Movie.get_by_id(movie.id)
    assert stored.score == 4.5
    assert stored.watched_count == 12
    assert stored.comment_count == 3
    assert heat_calls == [movie.id]
