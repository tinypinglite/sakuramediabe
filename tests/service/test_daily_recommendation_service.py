from datetime import date, datetime

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Actor,
    DailyRecommendationItem,
    HotReviewItem,
    Movie,
    MovieActor,
    MovieSimilarity,
    Playlist,
    PlaylistMovie,
    RankingItem,
    PLAYLIST_KIND_RECENTLY_PLAYED,
)
from src.service.discovery.daily_recommendation_service import DailyRecommendationService


def _create_movie(movie_number: str, javdb_id: str, **kwargs) -> Movie:
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_list_items_returns_empty_when_snapshot_missing(app):
    response = DailyRecommendationService.list_items(page=1, page_size=20)

    assert response.items == []
    assert response.total == 0


def test_list_items_rejects_invalid_page_size(app):
    with pytest.raises(ApiError) as exc_info:
        DailyRecommendationService.list_items(page=1, page_size=101)

    assert exc_info.value.code == "invalid_daily_recommendation_filter"


def test_generate_latest_snapshot_replaces_existing_rows(app):
    old_movie = _create_movie("ABP-001", "javdb-old")
    new_movie = _create_movie("ABP-002", "javdb-new", heat=100)
    DailyRecommendationItem.create(
        snapshot_date=date(2026, 5, 7),
        movie=old_movie,
        rank=1,
        score=1.0,
        reason_codes=["old"],
        reason_texts=["旧快照"],
        signal_scores={"heat": 1.0},
        generated_at=datetime(2026, 5, 7, 1, 0, 0),
    )

    stats = DailyRecommendationService.generate_latest_snapshot(
        target_date=date(2026, 5, 8),
        limit=1,
    )

    rows = list(DailyRecommendationItem.select().order_by(DailyRecommendationItem.rank.asc()))
    assert stats["stored_items"] == 1
    assert len(rows) == 1
    assert rows[0].movie_id == new_movie.id
    assert rows[0].snapshot_date == date(2026, 5, 8)
    assert rows[0].rank == 1


def test_generate_latest_snapshot_uses_public_cold_start_signals(app):
    hot_movie = _create_movie("ABP-001", "javdb-hot", heat=100)
    ranking_movie = _create_movie("ABP-002", "javdb-ranking", heat=0)
    _create_movie("ABP-003", "javdb-low", heat=0)
    RankingItem.create(
        source_key="javdb",
        board_key="censored",
        period="daily",
        rank=1,
        movie_number=ranking_movie.movie_number,
        movie=ranking_movie,
    )
    HotReviewItem.create(
        source_key="javdb",
        period="weekly",
        rank=1,
        review_id=1,
        movie_number=ranking_movie.movie_number,
        movie=ranking_movie,
    )

    stats = DailyRecommendationService.generate_latest_snapshot(target_date=date(2026, 5, 8), limit=3)
    rows = list(DailyRecommendationItem.select().order_by(DailyRecommendationItem.rank.asc()))

    assert stats["cold_start"] is True
    assert stats["extreme_cold_start"] is False
    assert rows[0].movie_id == hot_movie.id
    assert "popular_movie" in rows[0].reason_codes
    assert any("ranking_trending" in row.reason_codes for row in rows)
    assert any("hot_review" in row.reason_codes for row in rows)


def test_generate_latest_snapshot_uses_freshness_for_extreme_cold_start(app):
    older_movie = _create_movie(
        "ABP-001",
        "javdb-old",
        release_date=datetime(2025, 1, 1),
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )
    newer_movie = _create_movie(
        "ABP-002",
        "javdb-new",
        release_date=datetime(2026, 1, 1),
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )

    stats = DailyRecommendationService.generate_latest_snapshot(target_date=date(2026, 5, 8), limit=2)
    rows = list(DailyRecommendationItem.select().order_by(DailyRecommendationItem.rank.asc()))

    assert stats["extreme_cold_start"] is True
    assert [row.movie_id for row in rows] == [newer_movie.id, older_movie.id]
    assert rows[0].reason_codes == ["new_release"]


def test_generate_latest_snapshot_uses_similarity_from_recent_played_seed(app):
    source = _create_movie("ABP-001", "javdb-source")
    similar = _create_movie("ABP-002", "javdb-similar")
    unrelated = _create_movie("ABP-003", "javdb-unrelated")
    actor = Actor.create(name="Actor A", javdb_id="actor-a", is_subscribed=True)
    MovieActor.create(movie=source, actor=actor)
    playlist = Playlist.create(kind=PLAYLIST_KIND_RECENTLY_PLAYED, name="最近播放")
    PlaylistMovie.create(
        playlist=playlist,
        movie=source,
        updated_at=datetime(2026, 5, 8, 1, 0, 0),
    )
    MovieSimilarity.create(source_movie=source, target_movie=similar, score=0.9, rank=1)

    DailyRecommendationService.generate_latest_snapshot(target_date=date(2026, 5, 8), limit=3)
    rows = list(DailyRecommendationItem.select().order_by(DailyRecommendationItem.rank.asc()))
    row_by_movie_id = {row.movie_id: row for row in rows}

    assert row_by_movie_id[similar.id].rank < row_by_movie_id[unrelated.id].rank
    assert "similar_recent_play" in row_by_movie_id[similar.id].reason_codes


def test_list_items_returns_stale_snapshot_and_paginates(app, monkeypatch):
    monkeypatch.setattr(
        "src.service.discovery.daily_recommendation_service.runtime_now",
        lambda: datetime(2026, 5, 8, 8, 0, 0),
    )
    first_movie = _create_movie("ABP-001", "javdb-1")
    second_movie = _create_movie("ABP-002", "javdb-2")
    generated_at = datetime(2026, 5, 7, 5, 0, 0)
    DailyRecommendationItem.create(
        snapshot_date=date(2026, 5, 7),
        movie=first_movie,
        rank=1,
        score=0.8,
        reason_codes=["new_release"],
        reason_texts=["较新发布或近期入库"],
        signal_scores={"freshness": 1.0},
        generated_at=generated_at,
    )
    DailyRecommendationItem.create(
        snapshot_date=date(2026, 5, 7),
        movie=second_movie,
        rank=2,
        score=0.7,
        reason_codes=["new_release"],
        reason_texts=["较新发布或近期入库"],
        signal_scores={"freshness": 0.5},
        generated_at=generated_at,
    )

    response = DailyRecommendationService.list_items(page=2, page_size=1)

    assert response.total == 2
    assert response.items[0].movie_number == "ABP-002"
    assert response.items[0].rank == 2
    assert response.items[0].snapshot_date == date(2026, 5, 7)
    assert response.items[0].is_stale is True
    assert response.items[0].recommendation_score == 0.7
