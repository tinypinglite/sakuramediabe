import json
from datetime import datetime

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    Image,
    Media,
    Movie,
    MovieActor,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
)
from src.schema.metadata.javdb import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieTagResource,
)
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError
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
            "series_name": None,
            "cover_image": None,
            "release_date": None,
            "duration_minutes": 0,
            "score": 0.0,
            "watched_count": 0,
            "want_watch_count": 0,
            "comment_count": 0,
            "score_number": 0,
            "is_collection": False,
            "is_subscribed": False,
            "can_play": False,
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


def test_list_movies_supports_status_subscribed(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    cover_image = Image.create(
        origin="movies/ABP-123/cover-origin.jpg",
        small="movies/ABP-123/cover-small.jpg",
        medium="movies/ABP-123/cover-medium.jpg",
        large="movies/ABP-123/cover-large.jpg",
    )
    _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        cover_image=cover_image,
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
                "series_name": None,
                "cover_image": {
                    "id": cover_image.id,
                    "origin": build_signed_image_url("movies/ABP-123/cover-origin.jpg"),
                    "small": build_signed_image_url("movies/ABP-123/cover-small.jpg"),
                    "medium": build_signed_image_url("movies/ABP-123/cover-medium.jpg"),
                    "large": build_signed_image_url("movies/ABP-123/cover-large.jpg"),
                },
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_collection": False,
                "is_subscribed": True,
                "can_play": False,
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
                "series_name": None,
                "cover_image": None,
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


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
                "series_name": None,
                "cover_image": {
                    "id": latest_cover_image.id,
                    "origin": build_signed_image_url("latest/ABP-122/cover-origin.jpg"),
                    "small": build_signed_image_url("latest/ABP-122/cover-small.jpg"),
                    "medium": build_signed_image_url("latest/ABP-122/cover-medium.jpg"),
                    "large": build_signed_image_url("latest/ABP-122/cover-large.jpg"),
                },
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
            },
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABP-121",
                "title": "Movie 1",
                "series_name": None,
                "cover_image": None,
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
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
                "series_name": None,
                "cover_image": None,
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 0,
                "want_watch_count": 0,
                "comment_count": 0,
                "score_number": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": True,
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


def test_unsubscribing_movie_rejects_when_media_exists_without_delete_media(client, account_user):
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
            "message": "影片存在媒体文件，若需取消订阅请传 delete_media=true",
            "details": {
                "movie_number": "ABP-123",
                "media_count": 1,
                "delete_media_required": True,
            },
        }
    }
    assert Movie.get_by_id(movie.id).is_subscribed is True
    assert Media.get_by_id(media.id).valid is True


def test_unsubscribing_movie_with_delete_media_removes_files_and_invalidates_all_media(
    client,
    account_user,
    tmp_path,
):
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

    assert response.status_code == 204
    movie = Movie.get_by_id(movie.id)
    first_media = Media.get_by_id(first_media.id)
    second_media = Media.get_by_id(second_media.id)
    assert first_path.exists() is False
    assert second_path.exists() is False
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None
    assert first_media.valid is False
    assert second_media.valid is False


def test_unsubscribing_movie_with_delete_media_ignores_missing_files(client, account_user, tmp_path):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    existing_path = tmp_path / "abp-123-main.mp4"
    missing_path = tmp_path / "abp-123-missing.mp4"
    existing_path.write_bytes(b"main")
    first_media = Media.create(movie=movie, path=str(existing_path), valid=True)
    second_media = Media.create(movie=movie, path=str(missing_path), valid=True)

    response = client.delete(
        f"/movies/{movie.movie_number}/subscription?delete_media=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    movie = Movie.get_by_id(movie.id)
    first_media = Media.get_by_id(first_media.id)
    second_media = Media.get_by_id(second_media.id)
    assert existing_path.exists() is False
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None
    assert first_media.valid is False
    assert second_media.valid is False


def test_get_movie_detail_returns_series_name(client, account_user):
    token = _login(client, username=account_user.username)
    _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        series_name="Series 1",
        summary="summary",
    )

    response = client.get(
        "/movies/ABP-123",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["series_name"] == "Series 1"


def test_get_movie_detail_returns_signed_subtitles_for_media(
    client,
    account_user,
    tmp_path,
    build_signed_media_url,
    build_signed_subtitle_url,
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
    (version_dir / "ABP-123.srt").write_text("imported", encoding="utf-8")
    (version_dir / "ABP-123.zh.srt").write_text("manual", encoding="utf-8")
    media = Media.create(movie=movie, path=str(video_path), valid=True)

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
            "special_tags": "普通",
            "valid": True,
            "progress": None,
            "points": [],
            "subtitles": [
                {
                    "file_name": "ABP-123.srt",
                    "url": build_signed_subtitle_url(media.id, "ABP-123.srt"),
                },
                {
                    "file_name": "ABP-123.zh.srt",
                    "url": build_signed_subtitle_url(media.id, "ABP-123.zh.srt"),
                },
            ],
        }
    ]


def test_get_movie_detail_returns_playlist_summaries(client, account_user):
    token = _login(client, username=account_user.username)
    movie = _create_movie(
        "ABP-123",
        "MovieA1",
        title="Movie 1",
        summary="summary",
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
    assert events[-1]["data"]["movies"] == []
    assert events[-1]["data"]["stats"] == {
        "total": 1,
        "created_count": 0,
        "already_exists_count": 0,
        "failed_count": 1,
    }
