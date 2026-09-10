from src.service.discovery.moment_recommendation_service import (
    MomentRecommendationService,
)


def test_generate_recommendations_emits_visible_progress(test_db):
    payloads: list[dict] = []

    stats = MomentRecommendationService().generate_recommendations(
        progress_callback=payloads.append
    )

    assert stats["stored_items"] == 0
    assert payloads
    assert all(payload.get("text") for payload in payloads)
    assert all("current" in payload and "total" in payload for payload in payloads)
