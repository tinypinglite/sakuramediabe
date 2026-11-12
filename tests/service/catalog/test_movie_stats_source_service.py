from datetime import timedelta

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Movie
from src.plugins.extensions.stats import (
    PluginMovieStatsField,
    PluginMovieStatsSnapshot,
    PluginMovieStatsSource,
    validate_stats_snapshot,
)
from src.service.catalog.movie_stats_source_service import MovieStatsSourceService
from src.service.catalog.movie_stats_sync_service import MovieStatsSyncService


class _Reporter:
    def __init__(self):
        self.progress = []

    def emit(self, **kwargs):
        self.progress.append(kwargs)


def _install_source(monkeypatch, *, plugin_id: str, result):
    source = PluginMovieStatsSource(
        fields=[
            PluginMovieStatsField(
                key="score",
                value_type="float",
                label_i18n={"zh-CN": "评分", "en": "Score"},
                max=5,
            ),
            PluginMovieStatsField(
                key="watched_count",
                value_type="int",
                label_i18n={"zh-CN": "观看数", "en": "Watched"},
            ),
        ],
        heat_version="test-v1",
        fetch_stats=lambda _movie_number: result,
    )
    monkeypatch.setattr(
        MovieStatsSourceService,
        "sources",
        ((plugin_id, "Stats Plugin", {"en": "Stats Plugin"}, source),),
    )
    monkeypatch.setattr(settings.plugins, "enabled", [plugin_id])
    monkeypatch.setattr(settings.plugins, "stats_source", plugin_id)


def test_stats_snapshot_rejects_unknown_and_invalid_fields():
    source = PluginMovieStatsSource(
        fields=[
            PluginMovieStatsField(key="score", value_type="float", max=5),
        ],
        heat_version="v1",
        fetch_stats=lambda _number: None,
    )

    try:
        validate_stats_snapshot(
            source,
            PluginMovieStatsSnapshot(values={"unknown": 1}, heat=1),
        )
    except ValueError as exc:
        assert "未声明" in str(exc)
    else:
        raise AssertionError("unknown field must fail")

    try:
        validate_stats_snapshot(
            source,
            PluginMovieStatsSnapshot(values={"score": 6}, heat=1),
        )
    except ValueError as exc:
        assert "不能大于" in str(exc)
    else:
        raise AssertionError("out-of-range field must fail")


def test_stats_sync_writes_bound_source_snapshot(test_db, monkeypatch):
    movie = Movie.create(movie_number="STATS-SYNC-001", title="stats")
    _install_source(
        monkeypatch,
        plugin_id="stats_plugin",
        result=PluginMovieStatsSnapshot(
            values={"score": 4.5, "watched_count": 12},
            heat=3456,
        ),
    )
    reporter = _Reporter()

    result = MovieStatsSyncService.run(reporter=reporter)

    assert result["status"] == "ok"
    assert result["candidate_movies"] == 1
    assert result["succeeded_movies"] == 1
    stored = Movie.get_by_id(movie.id)
    assert stored.interaction_stats == {"score": 4.5, "watched_count": 12}
    assert stored.heat == 3456
    assert stored.stats_source == "stats_plugin"
    assert stored.heat_version == "test-v1"
    assert stored.stats_updated_at is not None
    assert reporter.progress


def test_stats_sync_rechecks_rows_with_stale_heat_version(test_db, monkeypatch):
    movie = Movie.create(
        movie_number="STATS-VERSION-001",
        title="stats version",
        release_date=utc_now_for_db().date() - timedelta(days=365),
        stats_source="stats_plugin",
        stats_updated_at=utc_now_for_db(),
        heat_version="old-v1",
    )
    _install_source(
        monkeypatch,
        plugin_id="stats_plugin",
        result=PluginMovieStatsSnapshot(values={"score": 4.8}, heat=4567),
    )

    result = MovieStatsSyncService.run(reporter=_Reporter())

    assert result["candidate_movies"] == 1
    stored = Movie.get_by_id(movie.id)
    assert stored.heat == 4567
    assert stored.heat_version == "test-v1"


def test_stats_sync_initializes_unsynced_old_movies(test_db, monkeypatch):
    movie = Movie.create(
        movie_number="STATS-OLD-001",
        title="old stats",
        release_date=utc_now_for_db().date() - timedelta(days=365),
    )
    _install_source(
        monkeypatch,
        plugin_id="stats_plugin",
        result=PluginMovieStatsSnapshot(values={"score": 4.8}, heat=4567),
    )

    result = MovieStatsSyncService.run(reporter=_Reporter())

    assert result["candidate_movies"] == 1
    stored = Movie.get_by_id(movie.id)
    assert stored.interaction_stats == {"score": 4.8}
    assert stored.heat == 4567
    assert stored.stats_updated_at is not None


def test_stats_sync_refreshes_old_movies_periodically(test_db, monkeypatch):
    movie = Movie.create(
        movie_number="STATS-OLD-SYNCED-001",
        title="old synced stats",
        release_date=utc_now_for_db().date() - timedelta(days=365),
        stats_source="stats_plugin",
        stats_updated_at=utc_now_for_db() - timedelta(days=31),
        heat_version="test-v1",
    )
    _install_source(
        monkeypatch,
        plugin_id="stats_plugin",
        result=PluginMovieStatsSnapshot(values={"score": 4.8}, heat=4567),
    )

    result = MovieStatsSyncService.run(reporter=_Reporter())

    assert result["candidate_movies"] == 1
    stored = Movie.get_by_id(movie.id)
    assert stored.interaction_stats == {"score": 4.8}
    assert stored.heat == 4567


def test_stats_sync_skips_when_no_source_bound(test_db, monkeypatch):
    monkeypatch.setattr(settings.plugins, "stats_source", None)

    result = MovieStatsSyncService.run(reporter=_Reporter())

    assert result["status"] == "skipped"
    assert result["reason"] == "stats_source_unavailable"
