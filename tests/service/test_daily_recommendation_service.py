import pytest

from src.service.discovery.daily_recommendation_service import (
    COLD_START_WEIGHTS,
    REGULAR_WEIGHTS,
)


def test_daily_recommendation_weights_remain_normalized_after_hot_review_removal():
    assert "hot_review" not in REGULAR_WEIGHTS
    assert "hot_review" not in COLD_START_WEIGHTS
    assert sum(REGULAR_WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(COLD_START_WEIGHTS.values()) == pytest.approx(1.0)
