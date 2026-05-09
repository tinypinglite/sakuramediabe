from datetime import datetime

from src.model import Image, Media, MediaThumbnail, MomentRecommendation, Movie
from src.service.discovery.moment_recommendation_service import STRATEGY_POPULAR


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


def _create_thumbnail(movie: Movie, offset: int):
    media = Media.create(movie=movie, path=f"/library/{movie.movie_number}.mp4", valid=True, special_tags="4K")
    image = Image.create(
        origin=f"movies/{movie.movie_number}/thumb-{offset}.webp",
        small=f"movies/{movie.movie_number}/thumb-{offset}.webp",
        medium=f"movies/{movie.movie_number}/thumb-{offset}.webp",
        large=f"movies/{movie.movie_number}/thumb-{offset}.webp",
    )
    return MediaThumbnail.create(media=media, image=image, offset=offset)


def test_moment_recommendations_requires_authentication(client):
    response = client.get("/moment-recommendations")

    assert response.status_code == 401


def test_moment_recommendations_returns_empty_page(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/moment-recommendations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": 1,
        "page_size": 20,
        "total": 0,
        "generated_at": None,
    }


def test_moment_recommendations_returns_current_pool(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-001", "javdb-abp001", title="Moment Movie", heat=10)
    thumbnail = _create_thumbnail(movie, 120)
    generated_at = datetime(2026, 5, 8, 4, 0, 0)
    row = MomentRecommendation.create(
        rank=1,
        score=0.88,
        strategy=STRATEGY_POPULAR,
        reason="来自热门影片的精选时刻",
        movie=movie,
        media=thumbnail.media,
        thumbnail=thumbnail,
        offset_seconds=thumbnail.offset,
        generated_at=generated_at,
    )

    response = client.get(
        "/moment-recommendations?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert payload["total"] == 1
    assert payload["generated_at"] == "2026-05-08T04:00:00"
    assert payload["items"] == [
        {
            "recommendation_id": row.id,
            "rank": 1,
            "score": 0.88,
            "strategy": STRATEGY_POPULAR,
            "reason": "来自热门影片的精选时刻",
            "media_id": thumbnail.media_id,
            "thumbnail_id": thumbnail.id,
            "offset_seconds": 120,
            "image": {
                "id": thumbnail.image_id,
                "origin": build_signed_image_url(thumbnail.image.origin),
                "small": build_signed_image_url(thumbnail.image.small),
                "medium": build_signed_image_url(thumbnail.image.medium),
                "large": build_signed_image_url(thumbnail.image.large),
            },
            "movie": {
                "javdb_id": "javdb-abp001",
                "movie_number": "ABP-001",
                "title": "Moment Movie",
                "title_zh": "",
                "series_id": None,
                "series_name": None,
                "cover_image": None,
                "thin_cover_image": None,
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 10,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
                "is_4k": True,
            },
        }
    ]


def test_moment_recommendations_rejects_invalid_page_size(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/moment-recommendations?page_size=101",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
