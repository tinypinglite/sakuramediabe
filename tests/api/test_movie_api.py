import json
from datetime import datetime

import pytest

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import (
    Actor,
    Image,
    Media,
    Movie,
    MovieActor,
    MovieSimilarity,
    MovieSeries,
    MovieTag,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
    Subtitle,
    Tag,
)
from sakuramedia_metadata_providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieListItemResource,
    JavdbMovieReviewResource,
    JavdbSeriesResource,
    JavdbMovieTagResource,
)
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError
from src.service.catalog.movie_service import MovieService


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


def _parse_sse_events(body: str):
    events = []
    current_event = None
    current_data = None
    for line in body.splitlines():
        if not line.strip():
            if current_event is not None:
                events.append({"event": current_event, "data": current_data})
            current_event = None
            current_data = None
            continue
        if line.startswith("event: "):
            current_event = line[len("event: ") :]
            continue
        if line.startswith("data: "):
            current_data = json.loads(line[len("data: ") :])

    if current_event is not None:
        events.append({"event": current_event, "data": current_data})
    return events


def _build_detail(movie_number: str) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        cover_image="https://example.com/cover.jpg",
        release_date="2024-05-20",
        duration_minutes=120,
        score=4.4,
        watched_count=11,
        want_watch_count=12,
        comment_count=13,
        score_number=14,
        is_subscribed=False,
        summary="summary",
        series_name="series",
        actors=[
            JavdbMovieActorResource(
                javdb_id=f"actor-{movie_number}",
                name="actor-a",
                avatar_url="https://example.com/actor-a.jpg",
                gender=1,
            )
        ],
        tags=[
            JavdbMovieTagResource(javdb_id=f"tag-{movie_number}", name="剧情"),
        ],
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )


def test_movie_parse_number_requires_auth(client):
    response = client.post("/movies/search/parse-number", json={"query": "ABP-123"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_parse_number_returns_parsed_number(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/search/parse-number",
        json={"query": "path/to/abp123.mp4"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "path/to/abp123.mp4",
        "parsed": True,
        "movie_number": "ABP-123",
        "reason": None,
    }


def test_movie_parse_number_returns_not_found_with_200(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/search/parse-number",
        json={"query": "no-number"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "query": "no-number",
        "parsed": False,
        "movie_number": None,
        "reason": "movie_number_not_found",
    }


def test_movie_parse_number_rejects_blank_query(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/search/parse-number",
        json={"query": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_movie_local_search_requires_auth(client):
    response = client.get("/movies/search/local?movie_number=ABP-123")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_local_search_matches_normalized_movie_number(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1")

    response = client.get(
        "/movies/search/local?movie_number=fc2-123456",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "FC2-PPV-123456",
            "title": "Movie 1",
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
            "heat": 0,
            "is_collection": False,
            "is_subscribed": False,
            "can_play": False,
            "is_4k": False,
        }
    ]


def test_movie_local_search_sets_can_play_true_when_valid_media_exists(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-123", "MovieA1", title="Movie 1")
    Media.create(
        movie=movie,
        path="/library/main/abp-123.mp4",
        valid=True,
    )

    response = client.get(
        "/movies/search/local?movie_number=ABP-123",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()[0]["can_play"] is True
    assert response.json()[0]["is_4k"] is False


def test_movie_collection_status_requires_auth(client):
    response = client.get("/movies/ABP-123/collection-status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_collection_status_returns_local_movie_state(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "MovieA1", title="Movie 1", is_collection=True)

    response = client.get(
        "/movies/ABP-123/collection-status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "movie_number": "ABP-123",
        "is_collection": True,
    }


def test_movie_collection_status_supports_normalized_lookup(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1", is_collection=False)

    response = client.get(
        "/movies/fc2-123456/collection-status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "movie_number": "FC2-PPV-123456",
        "is_collection": False,
    }


def test_movie_collection_status_returns_not_found_when_movie_missing(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies/ABP-404/collection-status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "movie_not_found",
            "message": "影片不存在",
            "details": {"movie_number": "ABP-404"},
        }
    }


def test_movie_reviews_requires_auth(client):
    response = client.get("/movies/ABP-123/reviews")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_similar_requires_auth(client):
    response = client.get("/movies/ABP-123/similar")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_missav_thumbnails_requires_auth(client):
    response = client.get("/movies/SSNI-888/thumbnails/missav/stream")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_missav_thumbnails_streams_progress_and_completed_result(
    client,
    account_user,
    monkeypatch,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    captured = {}

    def fake_stream(movie_number: str, *, refresh: bool = False):
        captured["movie_number"] = movie_number
        captured["refresh"] = refresh
        yield "search_started", {"movie_number": movie_number, "refresh": refresh}
        yield "manifest_resolved", {"movie_number": movie_number, "sprite_total": 1, "thumbnail_total": 2}
        yield "download_started", {"total": 1}
        yield "download_progress", {"completed": 1, "total": 1}
        yield "download_finished", {"completed": 1, "total": 1}
        yield "slice_started", {"total": 2}
        yield "slice_progress", {"completed": 2, "total": 2}
        yield "slice_finished", {"completed": 2, "total": 2}
        yield "completed", {
            "success": True,
            "result": {
                "movie_number": movie_number,
                "source": "missav",
                "total": 2,
                "items": [
                    {
                        "index": 0,
                        "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/0.jpg"),
                    },
                    {
                        "index": 1,
                        "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/1.jpg"),
                    },
                ],
            },
        }

    monkeypatch.setattr(MovieService, "stream_missav_thumbnails", fake_stream)

    response = client.get(
        "/movies/SSNI-888/thumbnails/missav/stream?refresh=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert captured == {"movie_number": "SSNI-888", "refresh": True}
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == [
        "search_started",
        "manifest_resolved",
        "download_started",
        "download_progress",
        "download_finished",
        "slice_started",
        "slice_progress",
        "slice_finished",
        "completed",
    ]
    assert events[-1]["data"] == {
        "success": True,
        "result": {
            "movie_number": "SSNI-888",
            "source": "missav",
            "total": 2,
            "items": [
                {
                    "index": 0,
                    "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/0.jpg"),
                },
                {
                    "index": 1,
                    "url": build_signed_image_url("movies/SSNI-888/missav-seek/frames/1.jpg"),
                },
            ],
        },
    }


def test_movie_missav_thumbnails_streams_failed_completed_event(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    monkeypatch.setattr(
        MovieService,
        "stream_missav_thumbnails",
        lambda movie_number, refresh=False: iter(
            [
                ("search_started", {"movie_number": movie_number, "refresh": refresh}),
                (
                    "completed",
                    {
                        "success": False,
                        "reason": "missav_thumbnail_not_found",
                        "detail": "thumbnail config missing",
                    },
                ),
            ]
        ),
    )

    response = client.get(
        "/movies/SSNI-888/thumbnails/missav/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == ["search_started", "completed"]
    assert events[-1]["data"] == {
        "success": False,
        "reason": "missav_thumbnail_not_found",
        "detail": "thumbnail config missing",
    }


def test_movie_reviews_returns_review_list_with_query_params(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")
    captured = {}

    class FakeProvider:
        def get_movie_reviews_by_javdb_id(
            self,
            javdb_id: str,
            page: int = 1,
            limit: int = 20,
            sort_by: str = "recently",
        ):
            captured["javdb_id"] = javdb_id
            captured["page"] = page
            captured["limit"] = limit
            captured["sort_by"] = sort_by
            return [
                JavdbMovieReviewResource(
                    id=1,
                    score=4,
                    content="很不错",
                    created_at="2026-03-10T08:00:00Z",
                    username="tester",
                    like_count=5,
                    watch_count=10,
                )
            ]

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.get(
        "/movies/ABP-123/reviews?page=2&page_size=5&sort=hotly",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": 1,
            "score": 4,
            "content": "很不错",
            "created_at": "2026-03-10T08:00:00Z",
            "username": "tester",
            "like_count": 5,
            "watch_count": 10,
            "movie": None,
        }
    ]
    assert captured == {
        "javdb_id": "javdb-ABP-123",
        "page": 2,
        "limit": 5,
        "sort_by": "hotly",
    }


def test_movie_reviews_returns_not_found_when_movie_missing(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies/ABP-404/reviews",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "movie_not_found",
            "message": "影片不存在",
            "details": {"movie_number": "ABP-404"},
        }
    }


def test_movie_reviews_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    response = client.get(
        "/movies/ABP-123/reviews?sort=latest",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_movie_reviews_rejects_invalid_page_params(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    invalid_page_response = client.get(
        "/movies/ABP-123/reviews?page=0",
        headers={"Authorization": f"Bearer {token}"},
    )
    invalid_page_size_response = client.get(
        "/movies/ABP-123/reviews?page_size=0",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert invalid_page_response.status_code == 422
    assert invalid_page_response.json()["error"]["code"] == "validation_error"
    assert invalid_page_size_response.status_code == 422
    assert invalid_page_size_response.json()["error"]["code"] == "validation_error"


def test_movie_reviews_maps_provider_request_error_to_502(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_reviews_by_javdb_id(
            self,
            javdb_id: str,
            page: int = 1,
            limit: int = 20,
            sort_by: str = "recently",
        ):
            raise MetadataRequestError("GET", "https://example.com/reviews", "upstream down")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.get(
        "/movies/ABP-123/reviews",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "movie_review_fetch_failed"
    assert response.json()["error"]["details"]["movie_number"] == "ABP-123"
    assert response.json()["error"]["details"]["javdb_id"] == "javdb-ABP-123"


def test_movie_similar_returns_ranked_movies_with_similarity_score(client, account_user):
    token = _login(client, username=account_user.username)
    source = _create_movie("FC2-PPV-123456", "MovieA1", title="Source")
    target = _create_movie("FC2-PPV-654321", "MovieA2", title="Target")
    Media.create(
        movie=target,
        path="/library/main/fc2-ppv-654321.mp4",
        valid=True,
        special_tags="4K 无码",
    )
    MovieSimilarity.create(source_movie=source, target_movie=target, score=0.91, rank=1)

    response = client.get(
        "/movies/fc2-123456/similar?limit=5",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "javdb_id": "MovieA2",
            "movie_number": "FC2-PPV-654321",
            "title": "Target",
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
            "heat": 0,
            "is_collection": False,
            "is_subscribed": False,
            "can_play": True,
            "is_4k": True,
            "similarity_score": 0.91,
        }
    ]


def test_movie_similar_returns_not_found_when_source_movie_missing(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies/ABP-404/similar",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "movie_not_found",
            "message": "影片不存在",
            "details": {"movie_number": "ABP-404"},
        }
    }


def test_list_movies_supports_status_subscribed(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    cover_image = Image.create(
        origin="movies/ABP-123/cover-origin.jpg",
        small="movies/ABP-123/cover-small.jpg",
        medium="movies/ABP-123/cover-medium.jpg",
        large="movies/ABP-123/cover-large.jpg",
    )
    thin_cover_image = Image.create(
        origin="movies/ABP-123/thin-origin.jpg",
        small="movies/ABP-123/thin-small.jpg",
        medium="movies/ABP-123/thin-medium.jpg",
        large="movies/ABP-123/thin-large.jpg",
    )
    _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        title_zh="中文标题 1",
        is_subscribed=True,
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
    )
    _create_movie("ABP-124", "MovieA2", title="Movie 2", is_subscribed=False)

    response = client.get(
        "/movies?status=subscribed",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-123",
                "title": "Movie 1",
                "title_zh": "中文标题 1",
                "series_id": None,
                "series_name": None,
                "cover_image": {
                    "id": cover_image.id,
                    "origin": build_signed_image_url("movies/ABP-123/cover-origin.jpg"),
                    "small": build_signed_image_url("movies/ABP-123/cover-small.jpg"),
                    "medium": build_signed_image_url("movies/ABP-123/cover-medium.jpg"),
                    "large": build_signed_image_url("movies/ABP-123/cover-large.jpg"),
                },
                "thin_cover_image": {
                    "id": thin_cover_image.id,
                    "origin": build_signed_image_url("movies/ABP-123/thin-origin.jpg"),
                    "small": build_signed_image_url("movies/ABP-123/thin-small.jpg"),
                    "medium": build_signed_image_url("movies/ABP-123/thin-medium.jpg"),
                    "large": build_signed_image_url("movies/ABP-123/thin-large.jpg"),
                },
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": True,
                "can_play": False,
                "is_4k": False,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


def test_list_movies_supports_status_playable_with_actor_filter(client, account_user):
    token = _login(client, username=account_user.username)
    matched_movie = _create_movie("ABP-123", "MovieA1", title="Movie 1")
    filtered_movie = _create_movie("ABP-124", "MovieA2", title="Movie 2")
    other_movie = _create_movie("ABP-125", "MovieA3", title="Movie 3")

    actor = Actor.create(name="三上悠亚", javdb_id="ActorA1", alias_name="")
    other_actor = Actor.create(name="鬼头桃菜", javdb_id="ActorA2", alias_name="")
    MovieActor.create(movie=matched_movie, actor=actor)
    MovieActor.create(movie=filtered_movie, actor=actor)
    MovieActor.create(movie=other_movie, actor=other_actor)
    Media.create(movie=matched_movie, path="/library/main/abp-123.mp4", valid=True)
    Media.create(movie=filtered_movie, path="/library/main/abp-124.mp4", valid=False)
    Media.create(movie=other_movie, path="/library/main/abp-125.mp4", valid=True)

    response = client.get(
        f"/movies?actor_id={actor.id}&status=playable",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-123",
                "title": "Movie 1",
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
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
                "is_4k": False,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


def test_list_movies_supports_special_tag_filter_and_is_4k(client, account_user):
    token = _login(client, username=account_user.username)
    matched_movie = _create_movie("ABP-123", "MovieA1", title="Movie 1")
    invalid_movie = _create_movie("ABP-124", "MovieA2", title="Movie 2")
    Media.create(movie=matched_movie, path="/library/main/abp-123.mp4", valid=True, special_tags="4K")
    Media.create(movie=invalid_movie, path="/library/main/abp-124.mp4", valid=False, special_tags="4K")

    response = client.get(
        "/movies?special_tag=4k",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABP-123",
            "title": "Movie 1",
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
            "heat": 0,
            "is_collection": False,
            "is_subscribed": False,
            "can_play": True,
            "is_4k": True,
        }
    ]
    assert response.json()["total"] == 1


def test_list_movies_rejects_invalid_special_tag(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?special_tag=normal",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_movies_rejects_invalid_status(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?status=unknown",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_movies_supports_collection_type_single(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-120", "MovieA1", title="Single Movie", is_collection=False)
    _create_movie("ABP-121", "MovieA2", title="Collection Movie", is_collection=True)

    response = client.get(
        "/movies?collection_type=single",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == ["ABP-120"]


def test_list_movies_supports_sort_release_date_desc(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-120", "MovieA1", title="Movie 1", release_date=datetime(2026, 3, 8, 9, 0, 0))
    _create_movie("ABP-121", "MovieA2", title="Movie 2", release_date=datetime(2026, 3, 10, 9, 0, 0))
    _create_movie("ABP-122", "MovieA3", title="Movie 3", release_date=None)

    response = client.get(
        "/movies?sort=release_date:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == [
        "ABP-121",
        "ABP-120",
        "ABP-122",
    ]


def test_list_movies_supports_sort_added_at_desc(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-120", "MovieA1", title="Movie 1")
    _create_movie("ABP-121", "MovieA2", title="Movie 2")

    response = client.get(
        "/movies?sort=added_at:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == ["ABP-121", "ABP-120"]


def test_list_movies_supports_combined_filters_and_subscribed_at_sort(client, account_user):
    token = _login(client, username=account_user.username)
    actor = Actor.create(name="三上悠亚", javdb_id="ActorA1", alias_name="")
    latest_movie = _create_movie(
        "ABP-120",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        is_collection=False,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    older_movie = _create_movie(
        "ABP-121",
        "MovieA2",
        title="Movie 2",
        is_subscribed=True,
        is_collection=False,
        subscribed_at=datetime(2026, 3, 8, 9, 0, 0),
    )
    _create_movie(
        "ABP-122",
        "MovieA3",
        title="Movie 3",
        is_subscribed=True,
        is_collection=True,
        subscribed_at=datetime(2026, 3, 11, 9, 0, 0),
    )
    _create_movie("ABP-123", "MovieA4", title="Movie 4", is_subscribed=False, is_collection=False)
    MovieActor.create(movie=latest_movie, actor=actor)
    MovieActor.create(movie=older_movie, actor=actor)

    response = client.get(
        f"/movies?actor_id={actor.id}&status=subscribed&collection_type=single&sort=subscribed_at:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == ["ABP-120", "ABP-121"]


def test_list_movies_supports_director_and_maker_exact_filters(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-120",
        "MovieA1",
        title="Movie 1",
        director_name="嵐山みちる",
        maker_name="S1 NO.1 STYLE",
    )
    _create_movie(
        "ABP-121",
        "MovieA2",
        title="Movie 2",
        director_name="嵐山みちる",
        maker_name="MOODYZ",
    )
    _create_movie(
        "ABP-122",
        "MovieA3",
        title="Movie 3",
        director_name="別导演",
        maker_name="S1 NO.1 STYLE",
    )

    response = client.get(
        "/movies?director_name=嵐山みちる&maker_name=S1%20NO.1%20STYLE",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == ["ABP-120"]


def test_list_movies_rejects_blank_director_or_maker_filter(client, account_user):
    token = _login(client, username=account_user.username)

    director_response = client.get(
        "/movies?director_name=%20%20%20",
        headers={"Authorization": f"Bearer {token}"},
    )
    maker_response = client.get(
        "/movies?maker_name=%20%20%20",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert director_response.status_code == 422
    assert director_response.json()["error"]["code"] == "invalid_movie_filter"
    assert maker_response.status_code == 422
    assert maker_response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_movies_rejects_invalid_collection_type(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?collection_type=unknown",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_movies_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?sort=id:desc",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_movies_rejects_invalid_tag_ids(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?tag_ids=1,a",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_movies_rejects_blank_tag_ids(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies?tag_ids=",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_movies_by_series_requires_auth(client):
    response = client.post("/movies/by-series", json={"series_id": 1})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_movies_by_series_returns_exact_matches(client, account_user):
    token = _login(client, username=account_user.username)
    series_movie = _create_movie("ABP-120", "MovieA1", title="Movie 1", series_name="A 系列")
    _create_movie("ABP-121", "MovieA2", title="Movie 2", series_name="A 系列")
    _create_movie("ABP-122", "MovieA3", title="Movie 3", series_name="A 系列")
    _create_movie("ABP-123", "MovieA4", title="Movie 4", series_name="B 系列")
    _create_movie("ABP-124", "MovieA5", title="Movie 5", series_name=None)

    response = client.post(
        "/movies/by-series",
        json={"series_id": series_movie.series_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert {item["movie_number"] for item in body["items"]} == {"ABP-120", "ABP-121", "ABP-122"}
    assert all(item["series_name"] == "A 系列" for item in body["items"])
    assert all(isinstance(item["series_id"], int) for item in body["items"])


def test_list_movies_by_series_supports_pagination(client, account_user):
    token = _login(client, username=account_user.username)
    series_movie = _create_movie("ABP-120", "MovieA1", title="Movie 1", series_name="A 系列")
    _create_movie("ABP-121", "MovieA2", title="Movie 2", series_name="A 系列")
    _create_movie("ABP-122", "MovieA3", title="Movie 3", series_name="A 系列")

    response = client.post(
        "/movies/by-series",
        json={"series_id": series_movie.series_id, "page": 1, "page_size": 2},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2


def test_list_movies_by_series_supports_sort_release_date_desc(client, account_user):
    token = _login(client, username=account_user.username)
    series_movie = _create_movie(
        "ABP-120", "MovieA1", title="Movie 1",
        series_name="A 系列", release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    _create_movie(
        "ABP-121", "MovieA2", title="Movie 2",
        series_name="A 系列", release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    _create_movie(
        "ABP-122", "MovieA3", title="Movie 3",
        series_name="A 系列", release_date=None,
    )

    response = client.post(
        "/movies/by-series",
        json={"series_id": series_movie.series_id, "sort": "release_date:desc"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert [item["movie_number"] for item in response.json()["items"]] == [
        "ABP-121",
        "ABP-120",
        "ABP-122",
    ]


def test_list_movies_by_series_returns_empty_when_no_match(client, account_user):
    token = _login(client, username=account_user.username)
    series_movie = _create_movie("ABP-120", "MovieA1", title="Movie 1", series_name="A 系列")

    response = client.post(
        "/movies/by-series",
        json={"series_id": series_movie.series_id + 1000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_movies_by_series_rejects_invalid_series_id(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/by-series",
        json={"series_id": 0},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


def test_list_movies_by_series_rejects_invalid_sort(client, account_user):
    token = _login(client, username=account_user.username)
    series_movie = _create_movie("ABP-120", "MovieA1", title="Movie 1", series_name="A 系列")

    response = client.post(
        "/movies/by-series",
        json={"series_id": series_movie.series_id, "sort": "bad"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"


def test_list_latest_movies_requires_auth(client):
    response = client.get("/movies/latest")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_latest_movies_returns_latest_media_movies(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    no_media_movie = _create_movie("ABP-120", "MovieA0", title="Movie 0")
    older_movie = _create_movie("ABP-121", "MovieA1", title="Movie 1")
    latest_cover_image = Image.create(
        origin="latest/ABP-122/cover-origin.jpg",
        small="latest/ABP-122/cover-small.jpg",
        medium="latest/ABP-122/cover-medium.jpg",
        large="latest/ABP-122/cover-large.jpg",
    )
    latest_movie = _create_movie(
        "ABP-122",
        "MovieA2",
        title="Movie 2",
        cover_image=latest_cover_image,
    )

    older_media = Media.create(movie=older_movie, path="/library/main/abp-121.mp4", valid=False)
    first_latest_media = Media.create(movie=latest_movie, path="/library/main/abp-122-a.mp4", valid=False)
    second_latest_media = Media.create(movie=latest_movie, path="/library/main/abp-122-b.mp4", valid=True)

    Media.update(created_at="2026-03-08 09:00:00").where(Media.id == older_media.id).execute()
    Media.update(created_at="2026-03-09 09:00:00").where(Media.id == first_latest_media.id).execute()
    Media.update(created_at="2026-03-10 09:00:00").where(Media.id == second_latest_media.id).execute()

    response = client.get(
        "/movies/latest?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABP-122",
                "title": "Movie 2",
                "title_zh": "",
                "series_id": None,
                "series_name": None,
                "cover_image": {
                    "id": latest_cover_image.id,
                    "origin": build_signed_image_url("latest/ABP-122/cover-origin.jpg"),
                    "small": build_signed_image_url("latest/ABP-122/cover-small.jpg"),
                    "medium": build_signed_image_url("latest/ABP-122/cover-medium.jpg"),
                    "large": build_signed_image_url("latest/ABP-122/cover-large.jpg"),
                },
                "thin_cover_image": None,
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
                "is_4k": False,
            },
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-121",
                "title": "Movie 1",
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
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
                "is_4k": False,
            },
        ],
        "page": 1,
        "page_size": 10,
        "total": 2,
    }
    assert no_media_movie.movie_number not in [item["movie_number"] for item in response.json()["items"]]


def test_list_latest_movies_supports_pagination(client, account_user):
    token = _login(client, username=account_user.username)
    first_movie = _create_movie("ABP-123", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABP-124", "MovieA2", title="Movie 2")

    first_media = Media.create(movie=first_movie, path="/library/main/abp-123.mp4", valid=True)
    second_media = Media.create(movie=second_movie, path="/library/main/abp-124.mp4", valid=True)

    Media.update(created_at="2026-03-08 09:00:00").where(Media.id == first_media.id).execute()
    Media.update(created_at="2026-03-09 09:00:00").where(Media.id == second_media.id).execute()

    response = client.get(
        "/movies/latest?page=2&page_size=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-123",
                "title": "Movie 1",
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
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
                "is_4k": False,
            }
        ],
        "page": 2,
        "page_size": 1,
        "total": 2,
    }


def test_list_subscribed_actor_latest_movies_requires_auth(client):
    response = client.get("/movies/subscribed-actors/latest")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_list_subscribed_actor_latest_movies_returns_filtered_sorted_and_deduplicated_movies(
    client,
    account_user,
):
    token = _login(client, username=account_user.username)
    subscribed_actor_a = Actor.create(name="三上悠亚", javdb_id="ActorA1", alias_name="", is_subscribed=True)
    subscribed_actor_b = Actor.create(name="河北彩花", javdb_id="ActorA2", alias_name="", is_subscribed=True)
    unsubscribed_actor = Actor.create(name="鬼头桃菜", javdb_id="ActorA3", alias_name="", is_subscribed=False)

    latest_movie = _create_movie(
        "ABP-130",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    older_movie = _create_movie(
        "ABP-131",
        "MovieA2",
        title="Movie 2",
        release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    no_release_date_movie = _create_movie("ABP-132", "MovieA3", title="Movie 3", release_date=None)
    unsubscribed_actor_movie = _create_movie(
        "ABP-133",
        "MovieA4",
        title="Movie 4",
        release_date=datetime(2026, 3, 11, 9, 0, 0),
    )
    collection_movie = _create_movie(
        "ABP-134",
        "MovieA5",
        title="Movie 5",
        release_date=datetime(2026, 3, 12, 9, 0, 0),
        is_collection=True,
    )

    MovieActor.create(movie=latest_movie, actor=subscribed_actor_a)
    MovieActor.create(movie=latest_movie, actor=subscribed_actor_b)
    MovieActor.create(movie=older_movie, actor=subscribed_actor_a)
    MovieActor.create(movie=no_release_date_movie, actor=subscribed_actor_b)
    MovieActor.create(movie=unsubscribed_actor_movie, actor=unsubscribed_actor)
    MovieActor.create(movie=collection_movie, actor=subscribed_actor_a)
    Media.create(movie=older_movie, path="/library/main/abp-131.mp4", valid=True)

    response = client.get(
        "/movies/subscribed-actors/latest?page=1&page_size=10",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-130",
                "title": "Movie 1",
                "title_zh": "",
                "series_id": None,
                "series_name": None,
                "cover_image": None,
                "thin_cover_image": None,
                "release_date": "2026-03-10",
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
                "is_4k": False,
            },
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABP-131",
                "title": "Movie 2",
                "title_zh": "",
                "series_id": None,
                "series_name": None,
                "cover_image": None,
                "thin_cover_image": None,
                "release_date": "2026-03-08",
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
                "is_4k": False,
            },
            {
                "javdb_id": "MovieA3",
                "movie_number": "ABP-132",
                "title": "Movie 3",
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
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
                "is_4k": False,
            },
        ],
        "page": 1,
        "page_size": 10,
        "total": 3,
    }


def test_list_subscribed_actor_latest_movies_supports_pagination(client, account_user):
    token = _login(client, username=account_user.username)
    subscribed_actor = Actor.create(name="三上悠亚", javdb_id="ActorA1", alias_name="", is_subscribed=True)
    first_movie = _create_movie(
        "ABP-130",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    second_movie = _create_movie(
        "ABP-131",
        "MovieA2",
        title="Movie 2",
        release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    collection_movie = _create_movie(
        "ABP-132",
        "MovieA3",
        title="Movie 3",
        release_date=datetime(2026, 3, 11, 9, 0, 0),
        is_collection=True,
    )
    MovieActor.create(movie=first_movie, actor=subscribed_actor)
    MovieActor.create(movie=second_movie, actor=subscribed_actor)
    MovieActor.create(movie=collection_movie, actor=subscribed_actor)

    response = client.get(
        "/movies/subscribed-actors/latest?page=2&page_size=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABP-131",
                "title": "Movie 2",
                "title_zh": "",
                "series_id": None,
                "series_name": None,
                "cover_image": None,
                "thin_cover_image": None,
                "release_date": "2026-03-08",
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
                "is_4k": False,
            }
        ],
        "page": 2,
        "page_size": 1,
        "total": 2,
    }


def test_movie_subscribe_requires_auth(client):
    response = client.put("/movies/ABP-123/subscription")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_collection_type_patch_requires_auth(client):
    response = client.patch(
        "/movies/collection-type",
        json={"movie_numbers": ["ABP-123"], "collection_type": "collection"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_collection_type_patch_marks_single_movie_as_collection(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-123", "MovieA1", title="Movie 1", is_collection=False)

    response = client.patch(
        "/movies/collection-type",
        headers={"Authorization": f"Bearer {token}"},
        json={"movie_numbers": ["ABP-123"], "collection_type": "collection"},
    )

    assert response.status_code == 200
    assert response.json() == {"requested_count": 1, "updated_count": 1}
    movie = Movie.get_by_id(movie.id)
    assert movie.is_collection is True
    assert movie.is_collection_overridden is True


def test_movie_collection_type_patch_updates_multiple_movies_and_ignores_missing_movie_numbers(
    client,
    account_user,
):
    token = _login(client, username=account_user.username)
    first_movie = _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1", is_collection=True)
    second_movie = _create_movie("ABP-123", "MovieA2", title="Movie 2", is_collection=True)

    response = client.patch(
        "/movies/collection-type",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "movie_numbers": ["fc2-123456", "ABP-123", "ABP-404", "ABP-123"],
            "collection_type": "single",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"requested_count": 4, "updated_count": 2}
    first_movie = Movie.get_by_id(first_movie.id)
    second_movie = Movie.get_by_id(second_movie.id)
    assert first_movie.is_collection is False
    assert first_movie.is_collection_overridden is True
    assert second_movie.is_collection is False
    assert second_movie.is_collection_overridden is True


def test_movie_unsubscribe_requires_auth(client):
    response = client.delete("/movies/ABP-123/subscription")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_subscribing_movie_updates_only_target_record(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-123", "MovieA1", title="Movie 1", is_subscribed=False, subscribed_at=None)
    other_movie = _create_movie("ABP-124", "MovieA2", title="Movie 2", is_subscribed=False, subscribed_at=None)

    response = client.put(
        f"/movies/{movie.movie_number}/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert Movie.get_by_id(movie.id).is_subscribed is True
    assert Movie.get_by_id(movie.id).subscribed_at is not None
    assert Movie.get_by_id(other_movie.id).is_subscribed is False
    assert Movie.get_by_id(other_movie.id).subscribed_at is None


def test_subscribing_movie_returns_not_found(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.put(
        "/movies/ABP-404/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "movie_not_found",
            "message": "影片不存在",
            "details": {"movie_number": "ABP-404"},
        }
    }


def test_unsubscribing_movie_without_media_clears_subscription(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )

    response = client.delete(
        f"/movies/{movie.movie_number}/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    movie = Movie.get_by_id(movie.id)
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None


def test_unsubscribing_movie_rejects_when_media_exists(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    media = Media.create(movie=movie, path="/library/main/abp-123.mp4", valid=True)

    response = client.delete(
        f"/movies/{movie.movie_number}/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "movie_subscription_has_media",
            "message": "影片存在媒体文件，无法取消订阅",
            "details": {
                "movie_number": "ABP-123",
                "media_count": 1,
            },
        }
    }
    assert Movie.get_by_id(movie.id).is_subscribed is True
    assert Media.get_by_id(media.id).valid is True


def test_unsubscribing_movie_ignores_delete_media_query_and_still_rejects(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    first_path = tmp_path / "abp-123-main.mp4"
    second_path = tmp_path / "abp-123-backup.mp4"
    first_path.write_bytes(b"main")
    second_path.write_bytes(b"backup")
    first_media = Media.create(movie=movie, path=str(first_path), valid=True)
    second_media = Media.create(movie=movie, path=str(second_path), valid=False)

    response = client.delete(
        f"/movies/{movie.movie_number}/subscription?delete_media=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert first_path.exists() is True
    assert second_path.exists() is True
    movie = Movie.get_by_id(movie.id)
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None
    assert Media.get_by_id(first_media.id).valid is True
    assert Media.get_by_id(second_media.id).valid is False


def test_get_movie_detail_returns_series_name(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        series_name="Series 1",
        maker_name="S1 NO.1 STYLE",
        director_name="嵐山みちる",
        summary="summary",
        heat=7,
    )

    response = client.get(
        "/movies/ABP-123",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["series_name"] == "Series 1"
    assert isinstance(response.json()["series_id"], int)
    assert response.json()["maker_name"] == "S1 NO.1 STYLE"
    assert response.json()["director_name"] == "嵐山みちる"
    assert response.json()["heat"] == 7


def test_get_movie_detail_does_not_embed_subtitles_in_media_items(
    client,
    account_user,
    tmp_path,
    build_signed_media_url,
):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        summary="summary",
    )
    version_dir = tmp_path / "ABP-123" / "1730000000000"
    version_dir.mkdir(parents=True)
    video_path = version_dir / "ABP-123.mp4"
    video_path.write_bytes(b"video")
    video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 120},
        "video": {"codec_name": "h264", "profile": "Main"},
        "audio": {"codec_name": "aac"},
        "subtitles": [],
    }
    media = Media.create(movie=movie, path=str(video_path), video_info=video_info, valid=True)

    response = client.get(
        "/movies/ABP-123",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["media_items"] == [
        {
            "media_id": media.id,
            "library_id": None,
            "play_url": build_signed_media_url(media.id),
            "storage_mode": None,
            "resolution": None,
            "file_size_bytes": 0,
            "duration_seconds": 0,
            "video_info": video_info,
            "special_tags": "普通",
            "valid": True,
            "progress": None,
            "points": [],
        }
    ]
    assert "subtitles" not in response.json()["media_items"][0]


def test_get_movie_subtitles_returns_signed_items(
    client,
    account_user,
    monkeypatch,
    tmp_path,
    build_signed_subtitle_url,
):
    token = _login(client, username=account_user.username)
    monkeypatch.setattr("src.config.config.settings.media.subtitle_root_path", str(tmp_path / "subtitles"))
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        summary="summary",
    )
    subtitle_path = tmp_path / "subtitles" / "ABP-123" / "ABP-123.srt"
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path.write_text("subtitle", encoding="utf-8")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))

    response = client.get(
        "/movies/ABP-123/subtitles",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["movie_number"] == "ABP-123"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["subtitle_id"] == subtitle.id
    assert payload["items"][0]["url"] == build_signed_subtitle_url(subtitle.id)
    assert payload["items"][0]["file_name"] == "ABP-123.srt"
    assert isinstance(payload["items"][0]["created_at"], str)


def test_get_movie_subtitles_returns_404_when_movie_not_found(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movies/NOT-FOUND/subtitles",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "movie_not_found"


def test_get_movie_subtitles_returns_empty_items_when_movie_has_no_subtitles(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-124",
        "MovieA2",
        title="Movie 2",
        summary="summary",
    )

    response = client.get(
        "/movies/ABP-124/subtitles",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["movie_number"] == "ABP-124"
    assert payload["items"] == []


def test_get_movie_detail_returns_playlist_summaries(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        summary="summary",
        desc="dmm desc",
        desc_zh="中文简介",
    )
    recent_playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    custom_playlist = Playlist.create(name="我的收藏", description="Favorite")
    PlaylistMovie.create(playlist=custom_playlist, movie=movie)
    PlaylistMovie.create(playlist=recent_playlist, movie=movie)

    response = client.get(
        "/movies/ABP-123",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["desc"] == "dmm desc"
    assert response.json()["desc_zh"] == "中文简介"
    assert response.json()["playlists"] == [
        {
            "id": recent_playlist.id,
            "name": "最近播放",
            "kind": PLAYLIST_KIND_RECENTLY_PLAYED,
            "is_system": True,
        },
        {
            "id": custom_playlist.id,
            "name": "我的收藏",
            "kind": "custom",
            "is_system": False,
        },
    ]


def test_movie_list_does_not_return_desc(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-999", "MovieZ9", title="Movie 9", desc="hidden desc", desc_zh="hidden zh desc")

    response = client.get(
        "/movies",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert "desc" not in response.json()["items"][0]
    assert "desc_zh" not in response.json()["items"][0]


def test_movie_local_search_returns_empty_when_not_found(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "MovieA1", title="Movie 1")

    response = client.get(
        "/movies/search/local?movie_number=SSNI-404",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_movie_javdb_stream_requires_auth(client):
    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "ABP-123"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_movie_javdb_stream_rejects_blank_movie_number(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_movie_javdb_stream_created_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            return Movie.create(
                javdb_id=detail.javdb_id,
                movie_number=detail.movie_number,
                title=detail.title,
            )

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "ABP-123"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == [
        "search_started",
        "movie_found",
        "upsert_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1]["data"]["success"] is True
    assert len(events[-1]["data"]["movies"]) == 1
    assert events[-1]["data"]["stats"] == {
        "total": 1,
        "created_count": 1,
        "already_exists_count": 0,
        "failed_count": 0,
    }


def test_movie_javdb_stream_returns_existing_subscription_state_after_upsert(
    client, account_user, monkeypatch, tmp_path
):
    token = _login(client, username=account_user.username)
    original_timestamp = datetime(2026, 3, 8, 9, 0, 0)
    _create_movie(
        "ABP-123",
        "javdb-ABP-123",
        title="old-title",
        is_subscribed=True,
        subscribed_at=original_timestamp,
    )

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail(movie_number)
            detail.is_subscribed = None
            return detail

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(
        MovieService,
        "_build_catalog_import_service",
        lambda: CatalogImportService(image_downloader=lambda url, target_path: target_path.write_bytes(b"img")),
    )

    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "ABP-123"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert events[-1]["data"]["success"] is True
    assert events[-1]["data"]["movies"][0]["is_subscribed"] is True
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_timestamp


def test_movie_javdb_stream_not_found_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "ABP-404"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == ["search_started", "completed"]
    assert events[-1]["data"] == {
        "success": False,
        "reason": "movie_not_found",
        "movies": [],
    }


def test_movie_javdb_stream_failed_upsert_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            raise ImageDownloadError("download_failed")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/movies/search/javdb/stream",
        json={"movie_number": "ABP-123"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == [
        "search_started",
        "movie_found",
        "upsert_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1]["data"]["success"] is False
    assert events[-1]["data"]["reason"] == "internal_error"


def test_import_series_movies_from_javdb_stream_requires_auth(client):
    response = client.post("/movies/series/1/javdb/import/stream")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_import_series_movies_from_javdb_stream_local_series_not_found(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movies/series/999/javdb/import/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == ["search_started", "completed"]
    assert events[-1]["data"] == {
        "success": False,
        "reason": "local_series_not_found",
        "movies": [],
    }


def test_import_series_movies_from_javdb_stream_skips_existing_and_imports_new(
    client, account_user, monkeypatch
):
    token = _login(client, username=account_user.username)
    local_series = MovieSeries.create(name="A 系列")
    _create_movie("ABP-001", "javdb-existing", title="Existing", series_name="A 系列")

    class FakeProvider:
        def __init__(self):
            self.detail_ids = []

        def search_series(self, series_name: str):
            return [
                JavdbSeriesResource(javdb_id="wrong-series", javdb_type=0, name="A 系列 SP", videos_count=9),
                JavdbSeriesResource(javdb_id="series-1", javdb_type=3, name=series_name, videos_count=3),
            ]

        def get_series_movies(self, series_id: str, series_type: int = 0):
            assert series_id == "series-1"
            assert series_type == 3
            return [
                JavdbMovieListItemResource(
                    javdb_id="javdb-existing",
                    movie_number="ABP-001",
                    title="Existing Remote",
                    duration_minutes=120,
                ),
                JavdbMovieListItemResource(
                    javdb_id="javdb-new",
                    movie_number="ABP-002",
                    title="New Remote",
                    duration_minutes=120,
                ),
                JavdbMovieListItemResource(
                    javdb_id="javdb-new",
                    movie_number="ABP-002",
                    title="New Remote Duplicate",
                    duration_minutes=120,
                ),
            ]

        def get_movie_by_javdb_id(self, javdb_id: str):
            self.detail_ids.append(javdb_id)
            return _build_detail("ABP-002")

    fake_provider = FakeProvider()

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            return Movie.create(
                javdb_id=detail.javdb_id,
                movie_number=detail.movie_number,
                title=detail.title,
                series_name=detail.series_name,
            )

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: fake_provider)
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        f"/movies/series/{local_series.id}/javdb/import/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == [
        "search_started",
        "series_found",
        "javdb_series_found",
        "movie_found",
        "upsert_started",
        "movie_skipped",
        "movie_upsert_started",
        "movie_upsert_finished",
        "upsert_finished",
        "completed",
    ]
    assert fake_provider.detail_ids == ["javdb-new"]
    assert events[-2]["data"] == {
        "total": 2,
        "created_count": 1,
        "already_exists_count": 1,
        "failed_count": 0,
    }
    assert events[-1]["data"]["success"] is True
    assert events[-1]["data"]["skipped_items"] == [
        {"javdb_id": "javdb-existing", "movie_number": "ABP-001", "reason": "already_exists"}
    ]
    assert [item["movie_number"] for item in events[-1]["data"]["movies"]] == ["ABP-002"]


def test_import_series_movies_from_javdb_stream_requires_exact_javdb_series_match(
    client, account_user, monkeypatch
):
    token = _login(client, username=account_user.username)
    local_series = MovieSeries.create(name="A 系列")

    class FakeProvider:
        def search_series(self, series_name: str):
            return [JavdbSeriesResource(javdb_id="series-1", name="A 系列 SP", videos_count=1)]

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.post(
        f"/movies/series/{local_series.id}/javdb/import/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events] == ["search_started", "series_found", "completed"]
    assert events[-1]["data"] == {
        "success": False,
        "reason": "javdb_series_not_found",
        "movies": [],
    }


def test_import_series_movies_from_javdb_stream_continues_after_movie_failure(
    client, account_user, monkeypatch
):
    token = _login(client, username=account_user.username)
    local_series = MovieSeries.create(name="A 系列")

    class FakeProvider:
        def search_series(self, series_name: str):
            return [JavdbSeriesResource(javdb_id="series-1", name=series_name, videos_count=2)]

        def get_series_movies(self, series_id: str, series_type: int = 0):
            return [
                JavdbMovieListItemResource(
                    javdb_id="javdb-failed",
                    movie_number="ABP-001",
                    title="Failed Remote",
                    duration_minutes=120,
                ),
                JavdbMovieListItemResource(
                    javdb_id="javdb-ok",
                    movie_number="ABP-002",
                    title="OK Remote",
                    duration_minutes=120,
                ),
            ]

        def get_movie_by_javdb_id(self, javdb_id: str):
            detail = _build_detail("ABP-001" if javdb_id == "javdb-failed" else "ABP-002")
            detail.javdb_id = javdb_id
            return detail

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            if detail.javdb_id == "javdb-failed":
                raise ImageDownloadError("download_failed")
            return Movie.create(
                javdb_id=detail.javdb_id,
                movie_number=detail.movie_number,
                title=detail.title,
            )

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        f"/movies/series/{local_series.id}/javdb/import/stream",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert events[-2]["data"] == {
        "total": 2,
        "created_count": 1,
        "already_exists_count": 0,
        "failed_count": 1,
    }
    assert events[-1]["data"]["success"] is True
    assert events[-1]["data"]["failed_items"] == [
        {
            "javdb_id": "javdb-failed",
            "movie_number": "ABP-001",
            "reason": "image_download_failed",
            "detail": "download_failed",
        }
    ]


def test_movie_metadata_refresh_requires_auth(client):
    response = client.post("/movies/ABP-123/metadata-refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "path",
    [
        "/movies/ABP-123/desc-translation",
        "/movies/ABP-123/interaction-sync",
        "/movies/ABP-123/heat-recompute",
    ],
)
def test_movie_manual_actions_require_auth(client, path):
    response = client.post(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "path",
    [
        "/movies/ABP-404/desc-translation",
        "/movies/ABP-404/interaction-sync",
        "/movies/ABP-404/heat-recompute",
    ],
)
def test_movie_manual_actions_return_404_when_movie_is_missing(client, account_user, path):
    token = _login(client, username=account_user.username)

    response = client.post(path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "movie_not_found"


def test_translate_movie_desc_returns_updated_detail_and_matches_normalized_movie_number(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)
    _create_movie("FC2-PPV-123456", "javdb-FC2-PPV-123456", desc="原始简介", desc_zh="旧译文", title="Movie 1")
    captured = {}

    class FakeTranslationService:
        def translate_movie(self, movie):
            movie.desc_zh = "新译文"
            movie.save(only=[Movie.desc_zh])
            return {"movie_id": movie.id, "movie_number": movie.movie_number, "updated_movies": 1}

    def fake_run_task(*, task_key, trigger_type, func, **kwargs):
        captured["task_key"] = task_key
        captured["trigger_type"] = trigger_type
        return func(None)

    monkeypatch.setattr(MovieService, "_build_movie_desc_translation_service", lambda: FakeTranslationService())
    monkeypatch.setattr("src.service.catalog.movie_service.ActivityService.run_task", fake_run_task)

    response = client.post(
        "/movies/fc2-123456/desc-translation",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["movie_number"] == "FC2-PPV-123456"
    assert response.json()["desc_zh"] == "新译文"
    assert captured == {"task_key": "movie_desc_translation", "trigger_type": "manual"}


def test_translate_movie_desc_returns_422_when_desc_is_missing(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-124", "javdb-ABP-124", desc="", title="Movie 1")

    monkeypatch.setattr(
        "src.service.catalog.movie_service.ActivityService.run_task",
        lambda **kwargs: kwargs["func"](None),
    )

    response = client.post(
        "/movies/ABP-124/desc-translation",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "movie_desc_missing"


def test_translate_movie_desc_maps_translation_client_error(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-125", "javdb-ABP-125", desc="原始简介", title="Movie 1")

    class FakeTranslationService:
        def translate_movie(self, movie):
            raise MovieDescTranslationClientError(
                503,
                "movie_desc_translation_unavailable",
                "service unavailable",
            )

    monkeypatch.setattr(MovieService, "_build_movie_desc_translation_service", lambda: FakeTranslationService())
    monkeypatch.setattr(
        "src.service.catalog.movie_service.ActivityService.run_task",
        lambda **kwargs: kwargs["func"](None),
    )

    response = client.post(
        "/movies/ABP-125/desc-translation",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "movie_desc_translation_unavailable"


def test_sync_movie_interactions_returns_updated_detail(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-126",
        "javdb-ABP-126",
        desc="原始简介",
        title="Movie 1",
        score=1.1,
        score_number=1,
        watched_count=1,
        want_watch_count=1,
        comment_count=1,
        heat=0,
    )
    captured = {}

    class FakeInteractionService:
        def sync_movie(self, movie):
            movie.score = 4.8
            movie.score_number = 20
            movie.watched_count = 21
            movie.want_watch_count = 22
            movie.comment_count = 23
            movie.heat = 20
            movie.save(
                only=[
                    Movie.score,
                    Movie.score_number,
                    Movie.watched_count,
                    Movie.want_watch_count,
                    Movie.comment_count,
                    Movie.heat,
                ]
            )
            return {
                "movie_id": movie.id,
                "movie_number": movie.movie_number,
                "updated_movies": 1,
                "unchanged_movies": 0,
                "heat_updated_movies": 1,
            }

    def fake_run_task(*, task_key, trigger_type, func, **kwargs):
        captured["task_key"] = task_key
        captured["trigger_type"] = trigger_type
        return func(None)

    monkeypatch.setattr(MovieService, "_build_movie_interaction_sync_service", lambda: FakeInteractionService())
    monkeypatch.setattr("src.service.catalog.movie_service.ActivityService.run_task", fake_run_task)

    response = client.post(
        "/movies/ABP-126/interaction-sync",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["score_number"] == 20
    assert response.json()["want_watch_count"] == 22
    assert response.json()["comment_count"] == 23
    assert response.json()["heat"] == 20
    assert captured == {"task_key": "movie_interaction_sync", "trigger_type": "manual"}


def test_sync_movie_interactions_returns_422_when_javdb_id_is_missing(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-127", "", title="Movie 1")

    response = client.post(
        "/movies/ABP-127/interaction-sync",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "movie_javdb_id_missing"


def test_sync_movie_interactions_returns_502_when_sync_fails(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-128", "javdb-ABP-128", title="Movie 1")

    class FakeInteractionService:
        def sync_movie(self, movie):
            raise RuntimeError("boom")

    monkeypatch.setattr(MovieService, "_build_movie_interaction_sync_service", lambda: FakeInteractionService())
    monkeypatch.setattr(
        "src.service.catalog.movie_service.ActivityService.run_task",
        lambda **kwargs: kwargs["func"](None),
    )

    response = client.post(
        "/movies/ABP-128/interaction-sync",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "movie_interaction_sync_failed"


def test_recompute_movie_heat_returns_updated_detail(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-129",
        "javdb-ABP-129",
        title="Movie 1",
        want_watch_count=10,
        comment_count=2,
        score_number=3,
        heat=0,
    )
    captured = {}

    def fake_run_task(*, task_key, trigger_type, func, **kwargs):
        captured["task_key"] = task_key
        captured["trigger_type"] = trigger_type
        return func(None)

    monkeypatch.setattr("src.service.catalog.movie_service.ActivityService.run_task", fake_run_task)

    response = client.post(
        "/movies/ABP-129/heat-recompute",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["heat"] == 5
    assert captured == {"task_key": "movie_heat_update", "trigger_type": "manual"}


def test_movie_metadata_refresh_returns_updated_detail(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    movie = _create_movie("ABP-123", "javdb-ABP-123", title="old-title", desc="keep-desc", desc_zh="keep-desc-zh")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail(movie_number)
            detail.javdb_id = "javdb-ABP-123-remote"
            detail.title = "new-title"
            detail.summary = "new-summary"
            detail.extra = {"remote": "payload"}
            return detail

    class FakeCatalogImportService:
        def refresh_movie_metadata_strict(self, target_movie, detail):
            target_movie.javdb_id = detail.javdb_id
            target_movie.title = detail.title
            target_movie.summary = detail.summary
            target_movie.extra = detail.extra
            target_movie.save()
            return target_movie

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        f"/movies/{movie.movie_number}/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["movie_number"] == "ABP-123"
    assert response.json()["javdb_id"] == "javdb-ABP-123-remote"
    assert response.json()["title"] == "new-title"
    assert response.json()["summary"] == "new-summary"
    assert Movie.get_by_id(movie.id).extra == {"remote": "payload"}
    assert response.json()["desc"] == "keep-desc"
    assert response.json()["desc_zh"] == "keep-desc-zh"


def test_movie_metadata_refresh_matches_normalized_local_movie_number(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("FC2-PPV-123456", "javdb-FC2-PPV-123456", title="old-title")
    captured: dict[str, str] = {}

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            captured["provider_movie_number"] = movie_number
            detail = _build_detail("FC2-PPV-123456")
            detail.title = "normalized-title"
            return detail

    class FakeCatalogImportService:
        def refresh_movie_metadata_strict(self, target_movie, detail):
            target_movie.title = detail.title
            target_movie.save()
            return target_movie

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/movies/fc2-123456/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["movie_number"] == "FC2-PPV-123456"
    assert response.json()["title"] == "normalized-title"
    assert captured == {"provider_movie_number": "FC2-123456"}


def test_movie_metadata_refresh_returns_404_when_remote_metadata_missing(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.post(
        "/movies/ABP-123/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "movie_metadata_not_found"


def test_movie_metadata_refresh_returns_502_when_refresh_fails(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def refresh_movie_metadata_strict(self, movie, detail):
            raise ImageDownloadError("refresh failed")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/movies/ABP-123/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "movie_metadata_refresh_failed"


def test_movie_metadata_refresh_returns_409_when_remote_number_conflicts(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail("SSNI-888")
            detail.movie_number = "SSNI-888"
            return detail

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.post(
        "/movies/ABP-123/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "movie_metadata_number_conflict"


def test_movie_metadata_refresh_returns_409_when_remote_javdb_id_conflicts(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")
    _create_movie("ABP-456", "javdb-ABP-456-remote", title="Movie 2")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail("ABP-123")
            detail.javdb_id = "javdb-ABP-456-remote"
            return detail

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    response = client.post(
        "/movies/ABP-123/metadata-refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "movie_metadata_javdb_id_conflict"
    assert response.json()["error"]["details"] == {
        "movie_number": "ABP-123",
        "normalized_movie_number": "ABP-123",
        "current_javdb_id": "javdb-ABP-123",
        "remote_javdb_id": "javdb-ABP-456-remote",
        "conflicting_movie_number": "ABP-456",
    }
