from datetime import date, datetime

from src.model import DailyRecommendationItem, Media, Movie


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_daily_recommendations_requires_authentication(client):
    response = client.get("/daily-recommendations")

    assert response.status_code == 401


def test_daily_recommendations_returns_empty_page_without_snapshot(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/daily-recommendations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 1, "page_size": 20, "total": 0}


def test_daily_recommendations_returns_snapshot_items(client, account_user, monkeypatch):
    monkeypatch.setattr(
        "src.service.discovery.daily_recommendation_service.runtime_now",
        lambda: datetime(2026, 5, 8, 8, 0, 0),
    )
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-001", "javdb-abp001", title="Daily Movie")
    Media.create(movie=movie, path="/library/main/abp-001.mp4", valid=True, special_tags="4K")
    DailyRecommendationItem.create(
        snapshot_date=date(2026, 5, 7),
        movie=movie,
        rank=1,
        score=0.88,
        reason_codes=["new_release"],
        reason_texts=["较新发布或近期入库"],
        signal_scores={"freshness": 1.0},
        generated_at=datetime(2026, 5, 7, 5, 0, 0),
    )

    response = client.get(
        "/daily-recommendations?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert payload["total"] == 1
    assert payload["items"][0]["movie_number"] == "ABP-001"
    assert payload["items"][0]["can_play"] is True
    assert payload["items"][0]["is_4k"] is True
    assert payload["items"][0]["snapshot_date"] == "2026-05-07"
    assert payload["items"][0]["rank"] == 1
    assert payload["items"][0]["recommendation_score"] == 0.88
    assert payload["items"][0]["reason_codes"] == ["new_release"]
    assert payload["items"][0]["signal_scores"] == {"freshness": 1.0}
    assert payload["items"][0]["is_stale"] is True


def test_daily_recommendations_rejects_invalid_page_size(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/daily-recommendations?page_size=101",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
