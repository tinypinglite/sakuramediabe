from types import SimpleNamespace

from src.config.config import settings
from src.model import Movie
from src.plugins.extensions.reviews import PluginMovieReview
from src.plugins.extensions.stats import PluginMovieStatsSnapshot
from src.service.catalog.movie_review_source_service import MovieReviewSourceService
from src.service.catalog.movie_stats_service import MovieStatsService


def _install_review_sources(monkeypatch, sources):
    MovieReviewSourceService.sources = tuple(
        (plugin_id, plugin_id, {}, source)
        for plugin_id, source in sources
    )
    monkeypatch.setattr(settings.plugins, "enabled", [plugin_id for plugin_id, _ in sources])
    monkeypatch.setattr(settings.plugins, "review_source", None)


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


def test_review_source_uses_bound_plugin_only(monkeypatch):
    calls: list[str] = []

    class FirstSource:
        def fetch_reviews(self, *_args):
            calls.append("first")
            return [PluginMovieReview(id=1, content="first")]

    class BoundSource:
        def fetch_reviews(self, *_args):
            calls.append("bound")
            return [PluginMovieReview(id=2, content="bound")]

    _install_review_sources(
        monkeypatch,
        [("first", FirstSource()), ("bound", BoundSource())],
    )
    monkeypatch.setattr(settings.plugins, "review_source", "bound")

    result = MovieReviewSourceService.fetch(
        "ABP-001", page=1, page_size=20, sort="recently"
    )

    assert [item.id for item in result] == [2]
    assert calls == ["bound"]


def test_stats_snapshot_replaces_values_and_heat(test_db):
    movie = Movie.create(movie_number="STATS-001", title="stats")

    changed = MovieStatsService.apply_snapshot(
        movie.id,
        plugin_id="stats_plugin",
        snapshot=PluginMovieStatsSnapshot(
            values={"score": 4.5, "watched_count": 12, "comment_count": 3},
            heat=1234,
        ),
        heat_version="stats-v1",
    )

    assert changed is True
    stored = Movie.get_by_id(movie.id)
    assert stored.interaction_stats == {
        "score": 4.5,
        "watched_count": 12,
        "comment_count": 3,
    }
    assert stored.heat == 1234
    assert stored.stats_source == "stats_plugin"
    assert stored.heat_version == "stats-v1"
    assert stored.stats_updated_at is not None

    changed = MovieStatsService.apply_snapshot(
        movie.id,
        plugin_id="stats_plugin",
        snapshot=PluginMovieStatsSnapshot(values={"watched_count": 13}, heat=1300),
        heat_version="stats-v1",
    )

    assert changed is True
    stored = Movie.get_by_id(movie.id)
    assert stored.interaction_stats == {"watched_count": 13}
    assert stored.heat == 1300
