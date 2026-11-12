"""排行榜同步的本地番号命中回归。"""

import pytest

from src.common.service_helpers import find_movie_by_number
from src.model import Movie, RankingItem
from src.service.discovery.ranking_service import (
    RANKING_SOURCE_OWNERS,
    RANKING_SOURCES,
    RankingBoardDefinition,
    RankingSourceDefinition,
    RankingSyncService,
    register_plugin_ranking_sources,
)


@pytest.fixture
def ranking_registry():
    """排名来源注册表是进程级全局；测试结束后恢复原状。"""
    sources = dict(RANKING_SOURCES)
    owners = dict(RANKING_SOURCE_OWNERS)
    try:
        yield
    finally:
        RANKING_SOURCES.clear()
        RANKING_SOURCES.update(sources)
        RANKING_SOURCE_OWNERS.clear()
        RANKING_SOURCE_OWNERS.update(owners)


def _register_probe_source(source_number: str) -> None:
    register_plugin_ranking_sources(
        [
            RankingSourceDefinition(
                key="probe_source",
                name="Probe",
                boards=(
                    RankingBoardDefinition(
                        key="probe_board",
                        name="Probe",
                        supported_periods=("daily",),
                        fetch_numbers=lambda _period: [source_number],
                    ),
                ),
            )
        ],
        {"probe_source": "probe_plugin"},
    )


@pytest.mark.parametrize(
    ("stored_number", "source_number"),
    [
        ("FC2-PPV-5003943", "FC2-5003943"),
        ("FC2-5003943", "FC2-PPV-5003943"),
    ],
)
def test_sync_board_period_matches_equivalent_fc2_number(
    test_db, monkeypatch, ranking_registry, stored_number, source_number
):
    movie = Movie.create(movie_number=stored_number, title="fc2")
    _register_probe_source(source_number)

    def fail_import(self, movie_number):
        raise AssertionError(f"unexpected import: {movie_number}")

    monkeypatch.setattr(RankingSyncService, "_import_movie_by_number", fail_import)

    result = RankingSyncService().sync_board_period(
        "probe_source",
        "probe_board",
        "daily",
    )

    assert result["fetched_numbers"] == 1
    assert result["local_hit_movies"] == 1
    assert result["imported_movies"] == 0
    assert result["skipped_movies"] == 0
    assert result["stored_items"] == 1
    item = RankingItem.get()
    assert item.movie_id == movie.id
    assert item.movie_number == stored_number
    assert item.rank == 1


def test_find_movie_by_number_matches_fc2_alias(test_db):
    movie = Movie.create(movie_number="FC2-PPV-5003943", title="fc2")

    assert find_movie_by_number("FC2-5003943").id == movie.id
    assert find_movie_by_number("FC2-PPV-5003943").id == movie.id
