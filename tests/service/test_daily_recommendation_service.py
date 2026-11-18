from datetime import date

import pytest

from src.service.discovery.daily_recommendation_service import (
    COLD_START_WEIGHTS,
    REGULAR_WEIGHTS,
    DailyRecommendationService,
)


def test_daily_recommendation_weights_remain_normalized_after_hot_review_removal():
    assert "hot_review" not in REGULAR_WEIGHTS
    assert "hot_review" not in COLD_START_WEIGHTS
    assert sum(REGULAR_WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(COLD_START_WEIGHTS.values()) == pytest.approx(1.0)


def test_generate_latest_snapshot_emits_visible_progress(test_db, monkeypatch):
    monkeypatch.setattr(
        DailyRecommendationService,
        "_load_candidate_movies",
        classmethod(lambda cls: []),
    )
    payloads: list[dict] = []

    stats = DailyRecommendationService.generate_latest_snapshot(
        target_date=date(2026, 9, 11),
        progress_callback=payloads.append,
    )

    assert stats["candidate_movies"] == 0
    assert payloads
    assert all(payload.get("text") for payload in payloads)
    assert all("current" in payload and "total" in payload for payload in payloads)
