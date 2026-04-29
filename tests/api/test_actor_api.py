import json

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Tag,
)
from sakuramedia_metadata_providers.models import JavdbMovieActorResource
from src.service.catalog.actor_service import ActorService
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _create_image():
    return Image.create(
        origin="origin.jpg",
        small="small.jpg",
        medium="medium.jpg",
        large="large.jpg",
    )


def _create_thumbnail(media: Media, offset_seconds: int, suffix: str | None = None) -> MediaThumbnail:
    marker = suffix or str(offset_seconds)
    movie_number = media.movie.movie_number if hasattr(media.movie, "movie_number") else str(media.movie)
    image = Image.create(
        origin=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        small=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        medium=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
        large=f"movies/{movie_number}/media/fingerprint-{marker}/thumbnails/{offset_seconds}.webp",
    )
    return MediaThumbnail.create(media=media, image=image, offset=offset_seconds)


def _create_actor(name: str, javdb_id: str, **kwargs):
    payload = {
        "name": name,
        "javdb_id": javdb_id,
        "alias_name": kwargs.pop("alias_name", ""),
    }
    payload.update(kwargs)
    return Actor.create(**payload)


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_get_actors_returns_entity_items(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    image = _create_image()
    first = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
    )
    _create_actor("鬼头桃菜", "ActorB2", alias_name="三上悠亚 / 鬼头桃菜")
    _create_actor("河北彩花", "ActorC3")

    response = client.get("/actors", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": first.id,
                "javdb_id": "ActorA1",
                "name": "三上悠亚",
                "alias_name": "三上悠亚 / 鬼头桃菜",
                "profile_image": {
                    "id": image.id,
                    "origin": build_signed_image_url("origin.jpg"),
                    "small": build_signed_image_url("small.jpg"),
                    "medium": build_signed_image_url("medium.jpg"),
                    "large": build_signed_image_url("large.jpg"),
                },
                "is_subscribed": True,
            },
            {
                "id": first.id + 1,
                "javdb_id": "ActorB2",
                "name": "鬼头桃菜",
                "alias_name": "三上悠亚 / 鬼头桃菜",
                "profile_image": None,
                "is_subscribed": False,
            },
            {
                "id": first.id + 2,
                "javdb_id": "ActorC3",
                "name": "河北彩花",
                "alias_name": "",
                "profile_image": None,
                "is_subscribed": False,
            },
        ],
        "page": 1,
        "page_size": 20,
        "total": 3,
    }


def test_get_actor_detail_returns_single_actor(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    image = _create_image()
    actor = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
    )

    response = client.get(
        f"/actors/{actor.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": actor.id,
        "javdb_id": "ActorA1",
        "name": "三上悠亚",
        "alias_name": "三上悠亚 / 鬼头桃菜",
        "profile_image": {
            "id": image.id,
            "origin": build_signed_image_url("origin.jpg"),
            "small": build_signed_image_url("small.jpg"),
            "medium": build_signed_image_url("medium.jpg"),
            "large": build_signed_image_url("large.jpg"),
        },
        "is_subscribed": True,
    }


def test_get_actor_detail_returns_not_found_with_snake_case_error_details(client, account_user):
    token = _login(client, username=account_user.username)
    response = client.get(
        "/actors/99999",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "actor_not_found",
            "message": "演员不存在",
            "details": {"actor_id": 99999},
        }
    }


def test_get_actors_supports_gender_filters(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    image = _create_image()
    female_actor = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
        gender=1,
    )
    male_actor = _create_actor("森林原人", "ActorB2", is_subscribed=False, gender=2)
    _create_actor("未知演员", "ActorC3", is_subscribed=False, gender=0)

    female_response = client.get("/actors?gender=female", headers={"Authorization": f"Bearer {token}"})
    male_response = client.get("/actors?gender=male", headers={"Authorization": f"Bearer {token}"})

    assert female_response.status_code == 200
    assert female_response.json() == {
        "items": [
            {
                "id": female_actor.id,
                "javdb_id": "ActorA1",
                "name": "三上悠亚",
                "alias_name": "三上悠亚 / 鬼头桃菜",
                "profile_image": {
                    "id": image.id,
                    "origin": build_signed_image_url("origin.jpg"),
                    "small": build_signed_image_url("small.jpg"),
                    "medium": build_signed_image_url("medium.jpg"),
                    "large": build_signed_image_url("large.jpg"),
                },
                "is_subscribed": True,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }
    assert male_response.status_code == 200
    assert male_response.json()["items"] == [
        {
            "id": male_actor.id,
            "javdb_id": "ActorB2",
            "name": "森林原人",
            "alias_name": "",
            "profile_image": None,
            "is_subscribed": False,
        }
    ]


def test_get_actors_supports_subscription_status_filters(
    client,
    account_user,
    build_signed_image_url,
):
    token = _login(client, username=account_user.username)
    image = _create_image()
    subscribed_actor = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
    )
    unsubscribed_actor = _create_actor("鬼头桃菜", "ActorB2", is_subscribed=False)

    subscribed_response = client.get(
        "/actors?subscription_status=subscribed",
        headers={"Authorization": f"Bearer {token}"},
    )
    unsubscribed_response = client.get(
        "/actors?subscription_status=unsubscribed",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert subscribed_response.status_code == 200
    assert subscribed_response.json()["items"] == [
        {
            "id": subscribed_actor.id,
            "javdb_id": "ActorA1",
            "name": "三上悠亚",
            "alias_name": "三上悠亚 / 鬼头桃菜",
            "profile_image": {
                "id": image.id,
                "origin": build_signed_image_url("origin.jpg"),
                "small": build_signed_image_url("small.jpg"),
                "medium": build_signed_image_url("medium.jpg"),
                "large": build_signed_image_url("large.jpg"),
            },
            "is_subscribed": True,
        }
    ]
    assert unsubscribed_response.status_code == 200
    assert unsubscribed_response.json()["items"] == [
        {
            "id": unsubscribed_actor.id,
            "javdb_id": "ActorB2",
            "name": "鬼头桃菜",
            "alias_name": "",
            "profile_image": None,
            "is_subscribed": False,
        }
    ]


def test_get_actors_supports_combined_filters(client, account_user):
    token = _login(client, username=account_user.username)
    matched_actor = _create_actor("三上悠亚", "ActorA1", is_subscribed=True, gender=1)
    _create_actor("河北彩花", "ActorB2", is_subscribed=False, gender=1)
    _create_actor("森林原人", "ActorC3", is_subscribed=True, gender=2)

    response = client.get(
        "/actors?gender=female&subscription_status=subscribed",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": matched_actor.id,
                "javdb_id": "ActorA1",
                "name": "三上悠亚",
                "alias_name": "",
                "profile_image": None,
                "is_subscribed": True,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


def test_get_actors_rejects_invalid_filters(client, account_user):
    token = _login(client, username=account_user.username)

    invalid_gender = client.get("/actors?gender=unknown", headers={"Authorization": f"Bearer {token}"})
    invalid_subscription = client.get(
        "/actors?subscription_status=unknown",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert invalid_gender.status_code == 422
    assert invalid_subscription.status_code == 422


def test_get_actor_subscriptions_route_is_removed(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/actors/subscriptions",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


def test_subscribing_actor_updates_only_target_record(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1", is_subscribed=False)
    other_actor = _create_actor("鬼头桃菜", "ActorB2", is_subscribed=False)

    response = client.put(
        f"/actors/{actor.id}/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 204
    assert Actor.get_by_id(actor.id).is_subscribed is True
    assert Actor.get_by_id(other_actor.id).is_subscribed is False


def test_get_actor_movies_returns_movies_for_single_actor(client, account_user, build_signed_image_url):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")
    other_actor = _create_actor("鬼头桃菜", "ActorB2")
    thin_cover_image = Image.create(
        origin="actors/movie-thin-origin.jpg",
        small="actors/movie-thin-small.jpg",
        medium="actors/movie-thin-medium.jpg",
        large="actors/movie-thin-large.jpg",
    )
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        title_zh="演员作品中文标题",
        thin_cover_image=thin_cover_image,
    )
    other_movie = _create_movie("ABC-002", "MovieB2", title="Movie 2")
    MovieActor.create(movie=movie, actor=actor)
    MovieActor.create(movie=other_movie, actor=other_actor)

    response = client.get(
        f"/actors/{actor.id}/movies",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABC-001",
            "title": "Movie 1",
            "title_zh": "演员作品中文标题",
            "cover_image": None,
            "thin_cover_image": {
                "id": thin_cover_image.id,
                "origin": build_signed_image_url("actors/movie-thin-origin.jpg"),
                "small": build_signed_image_url("actors/movie-thin-small.jpg"),
                "medium": build_signed_image_url("actors/movie-thin-medium.jpg"),
                "large": build_signed_image_url("actors/movie-thin-large.jpg"),
            },
            "can_play": False,
            "is_4k": False,
        }
    ]


def test_get_actor_movies_supports_special_tag_filter_and_is_4k(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")
    matched_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    invalid_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    MovieActor.create(movie=matched_movie, actor=actor)
    MovieActor.create(movie=invalid_movie, actor=actor)
    Media.create(movie=matched_movie, path="/library/main/abc-001.mp4", valid=True, special_tags="4K")
    Media.create(movie=invalid_movie, path="/library/main/abc-002.mp4", valid=False, special_tags="4K")

    response = client.get(
        f"/actors/{actor.id}/movies?special_tag=4k",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABC-001",
            "title": "Movie 1",
            "title_zh": "",
            "cover_image": None,
            "thin_cover_image": None,
            "can_play": True,
            "is_4k": True,
        }
    ]
    assert response.json()["total"] == 1


def test_get_actor_movies_rejects_invalid_special_tag(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")

    response = client.get(
        f"/actors/{actor.id}/movies?special_tag=normal",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_actor_movie_ids_returns_linked_ids(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")
    movie_a = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    movie_b = _create_movie("ABC-002", "MovieB2", title="Movie 2")
    MovieActor.create(movie=movie_a, actor=actor)
    MovieActor.create(movie=movie_b, actor=actor)

    response = client.get(
        f"/actors/{actor.id}/movie-ids",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == [movie_a.id, movie_b.id]


def test_get_actor_tags_and_years(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")
    drama = Tag.create(name="剧情")
    uniform = Tag.create(name="制服")
    older_movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        release_date="2023-01-02 03:04:05",
    )
    newer_movie = _create_movie(
        "ABC-002",
        "MovieB2",
        title="Movie 2",
        release_date="2024-02-03 04:05:06",
    )
    MovieActor.create(movie=older_movie, actor=actor)
    MovieActor.create(movie=newer_movie, actor=actor)
    MovieTag.create(movie=older_movie, tag=drama)
    MovieTag.create(movie=newer_movie, tag=drama)
    MovieTag.create(movie=newer_movie, tag=uniform)

    headers = {"Authorization": f"Bearer {token}"}
    tags_response = client.get(f"/actors/{actor.id}/tags", headers=headers)
    years_response = client.get(f"/actors/{actor.id}/years", headers=headers)

    assert tags_response.status_code == 200
    assert sorted(tags_response.json(), key=lambda item: item["tag_id"]) == [
        {"tag_id": drama.id, "name": "剧情"},
        {"tag_id": uniform.id, "name": "制服"},
    ]
    assert years_response.status_code == 200
    assert years_response.json() == [{"year": 2024}, {"year": 2023}]


def test_get_movie_detail_returns_actor_entities(
    client,
    account_user,
    build_signed_image_url,
    build_signed_media_url,
):
    token = _login(client, username=account_user.username)
    actor_image = Image.create(
        origin="actor-origin.jpg",
        small="actor-small.jpg",
        medium="actor-medium.jpg",
        large="actor-large.jpg",
    )
    cover_image = Image.create(
        origin="cover-origin.jpg",
        small="cover-small.jpg",
        medium="cover-medium.jpg",
        large="cover-large.jpg",
    )
    thin_cover_image = Image.create(
        origin="thin-origin.jpg",
        small="thin-small.jpg",
        medium="thin-medium.jpg",
        large="thin-large.jpg",
    )
    plot_image = Image.create(
        origin="plot-origin.jpg",
        small="plot-small.jpg",
        medium="plot-medium.jpg",
        large="plot-large.jpg",
    )
    actor_a = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        gender=1,
        profile_image=actor_image,
    )
    actor_b = _create_actor("鬼头桃菜", "ActorB2", alias_name="三上悠亚 / 鬼头桃菜", gender=2)
    tag = Tag.create(name="剧情")
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        summary="summary",
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
    )
    MovieActor.create(movie=movie, actor=actor_a)
    MovieActor.create(movie=movie, actor=actor_b)
    MovieTag.create(movie=movie, tag=tag)
    MovieTag.create(movie=movie, tag=Tag.create(name="制服"))
    MoviePlotImage.create(movie=movie, image=plot_image)
    playable_media = Media.create(
        movie=movie,
        path="/library/main/abc-001-main.mp4",
        storage_mode="hardlink",
        resolution="1920x1080",
        file_size_bytes=100,
        duration_seconds=7200,
        special_tags="普通",
        valid=True,
    )
    backup_media = Media.create(
        movie=movie,
        path="/library/main/abc-001-backup.mp4",
        storage_mode="copy",
        resolution="1280x720",
        file_size_bytes=50,
        duration_seconds=3600,
        special_tags="无码",
        valid=False,
    )
    MediaProgress.create(
        media=playable_media,
        position_seconds=600,
        last_watched_at="2026-03-08 09:30:00",
    )
    first_thumbnail = _create_thumbnail(playable_media, offset_seconds=120, suffix="first")
    second_thumbnail = _create_thumbnail(playable_media, offset_seconds=360, suffix="second")
    backup_thumbnail = _create_thumbnail(backup_media, offset_seconds=90, suffix="backup")
    first_point = MediaPoint.create(media=playable_media, thumbnail=first_thumbnail, offset_seconds=120)
    second_point = MediaPoint.create(media=playable_media, thumbnail=second_thumbnail, offset_seconds=360)
    backup_point = MediaPoint.create(media=backup_media, thumbnail=backup_thumbnail, offset_seconds=90)

    response = client.get("/movies/ABC-001", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["cover_image"] == {
        "id": cover_image.id,
        "origin": build_signed_image_url("cover-origin.jpg"),
        "small": build_signed_image_url("cover-small.jpg"),
        "medium": build_signed_image_url("cover-medium.jpg"),
        "large": build_signed_image_url("cover-large.jpg"),
    }
    assert payload["thin_cover_image"] == {
        "id": thin_cover_image.id,
        "origin": build_signed_image_url("thin-origin.jpg"),
        "small": build_signed_image_url("thin-small.jpg"),
        "medium": build_signed_image_url("thin-medium.jpg"),
        "large": build_signed_image_url("thin-large.jpg"),
    }
    assert payload["plot_images"] == [
        {
            "id": plot_image.id,
            "origin": build_signed_image_url("plot-origin.jpg"),
            "small": build_signed_image_url("plot-small.jpg"),
            "medium": build_signed_image_url("plot-medium.jpg"),
            "large": build_signed_image_url("plot-large.jpg"),
        }
    ]
    assert payload["actors"] == [
        {
            "id": actor_a.id,
            "javdb_id": "ActorA1",
            "name": "三上悠亚",
            "alias_name": "三上悠亚 / 鬼头桃菜",
            "gender": 1,
            "is_subscribed": False,
            "profile_image": {
                "id": actor_image.id,
                "origin": build_signed_image_url("actor-origin.jpg"),
                "small": build_signed_image_url("actor-small.jpg"),
                "medium": build_signed_image_url("actor-medium.jpg"),
                "large": build_signed_image_url("actor-large.jpg"),
            },
        },
        {
            "id": actor_b.id,
            "javdb_id": "ActorB2",
            "name": "鬼头桃菜",
            "alias_name": "三上悠亚 / 鬼头桃菜",
            "gender": 2,
            "is_subscribed": False,
            "profile_image": None,
        },
    ]
    assert payload["can_play"] is True
    assert payload["is_4k"] is False
    assert payload["media_items"] == [
        {
            "media_id": playable_media.id,
            "library_id": None,
            "play_url": build_signed_media_url(playable_media.id),
            "storage_mode": "hardlink",
            "resolution": "1920x1080",
            "file_size_bytes": 100,
            "duration_seconds": 7200,
            "video_info": None,
            "special_tags": "普通",
            "valid": True,
            "progress": {
                "last_position_seconds": 600,
                "last_watched_at": "2026-03-08T09:30:00",
            },
            "points": [
                {
                    "point_id": first_point.id,
                    "thumbnail_id": first_thumbnail.id,
                    "offset_seconds": 120,
                    "image": {
                        "id": first_thumbnail.image_id,
                        "origin": build_signed_image_url(first_thumbnail.image.origin),
                        "small": build_signed_image_url(first_thumbnail.image.small),
                        "medium": build_signed_image_url(first_thumbnail.image.medium),
                        "large": build_signed_image_url(first_thumbnail.image.large),
                    },
                },
                {
                    "point_id": second_point.id,
                    "thumbnail_id": second_thumbnail.id,
                    "offset_seconds": 360,
                    "image": {
                        "id": second_thumbnail.image_id,
                        "origin": build_signed_image_url(second_thumbnail.image.origin),
                        "small": build_signed_image_url(second_thumbnail.image.small),
                        "medium": build_signed_image_url(second_thumbnail.image.medium),
                        "large": build_signed_image_url(second_thumbnail.image.large),
                    },
                },
            ],
        },
        {
            "media_id": backup_media.id,
            "library_id": None,
            "play_url": build_signed_media_url(backup_media.id),
            "storage_mode": "copy",
            "resolution": "1280x720",
            "file_size_bytes": 50,
            "duration_seconds": 3600,
            "video_info": None,
            "special_tags": "无码",
            "valid": False,
            "progress": None,
            "points": [
                {
                    "point_id": backup_point.id,
                    "thumbnail_id": backup_thumbnail.id,
                    "offset_seconds": 90,
                    "image": {
                        "id": backup_thumbnail.image_id,
                        "origin": build_signed_image_url(backup_thumbnail.image.origin),
                        "small": build_signed_image_url(backup_thumbnail.image.small),
                        "medium": build_signed_image_url(backup_thumbnail.image.medium),
                        "large": build_signed_image_url(backup_thumbnail.image.large),
                    },
                },
            ],
        },
    ]


def test_list_movies_supports_actor_id_filter(client, account_user):
    token = _login(client, username=account_user.username)
    actor = _create_actor("三上悠亚", "ActorA1")
    other_actor = _create_actor("鬼头桃菜", "ActorB2")
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    _create_movie("ABC-002", "MovieB2", title="Movie 2")
    MovieActor.create(movie=movie, actor=actor)
    MovieActor.create(movie=movie, actor=other_actor)
    Media.create(
        movie=movie,
        path="/library/main/abc-001.mp4",
        valid=True,
    )

    response = client.get(
        f"/movies?actor_id={actor.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABC-001",
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
    ]


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


def test_local_actor_search_requires_auth(client):
    response = client.get("/actors/search/local?query=三上")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_local_actor_search_matches_name_and_alias_name(client, account_user):
    token = _login(client, username=account_user.username)
    _create_actor("Mikami Yua", "ActorA1", alias_name="三上悠亚")
    _create_actor("Kana Momonogi", "ActorB2", alias_name="Guitou Taocai")
    _create_actor("Other Name", "ActorC3", alias_name="")

    name_response = client.get(
        "/actors/search/local?query=mIkAmI",
        headers={"Authorization": f"Bearer {token}"},
    )
    alias_response = client.get(
        "/actors/search/local?query=guitou",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert name_response.status_code == 200
    assert [item["javdb_id"] for item in name_response.json()] == ["ActorA1"]
    assert alias_response.status_code == 200
    assert [item["javdb_id"] for item in alias_response.json()] == ["ActorB2"]


def test_local_actor_search_returns_empty_list_when_no_match(client, account_user):
    token = _login(client, username=account_user.username)
    _create_actor("Mikami Yua", "ActorA1", alias_name="三上悠亚")

    response = client.get(
        "/actors/search/local?query=not-found",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_javdb_actor_stream_requires_auth(client):
    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_javdb_actor_stream_rejects_blank_actor_name(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_javdb_actor_stream_created_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def __init__(self, host, proxy=None, actor_image_resolver=None):
            self.host = host
            self.proxy = proxy

        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=f"{actor_name}-1",
                    avatar_url="https://c0.jdbstatic.com/avatars/a1.jpg",
                    gender=1,
                ),
                JavdbMovieActorResource(
                    javdb_id="ActorA2",
                    name=f"{actor_name}-2",
                    avatar_url=None,
                    gender=1,
                ),
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            actor = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id)
            if actor is None:
                actor = Actor.create(
                    javdb_id=actor_resource.javdb_id,
                    name=actor_resource.name,
                    alias_name=actor_resource.name,
                    gender=actor_resource.gender,
                )
            return actor

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider(None))
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse_events(response.text)
    assert [item["event"] for item in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "image_download_started",
        "image_download_finished",
        "upsert_finished",
        "completed",
    ]
    assert events[1]["data"]["total"] == 2
    assert len(events[1]["data"]["actors"]) == 2
    assert events[-2]["data"] == {
        "total": 2,
        "created_count": 2,
        "already_exists_count": 0,
        "failed_count": 0,
    }
    assert events[-1]["data"]["success"] is True
    assert len(events[-1]["data"]["actors"]) == 2
    assert events[-1]["data"]["failed_items"] == []
    actor = Actor.get(Actor.javdb_id == "ActorA1")
    assert actor.name == "三上悠亚-1"


def test_javdb_actor_stream_already_exists_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    _create_actor("Old Name", "ActorA1", alias_name="Old Name")

    class FakeProvider:
        def __init__(self, host, proxy=None, actor_image_resolver=None):
            self.host = host
            self.proxy = proxy

        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=actor_name,
                    avatar_url=None,
                    gender=1,
                )
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            actor = Actor.get(Actor.javdb_id == actor_resource.javdb_id)
            actor.name = actor_resource.name
            actor.save()
            return actor

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider(None))
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [item["event"] for item in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "upsert_finished",
        "completed",
    ]
    assert events[-2]["data"] == {
        "total": 1,
        "created_count": 0,
        "already_exists_count": 1,
        "failed_count": 0,
    }
    assert events[-1]["data"]["success"] is True
    assert [item["javdb_id"] for item in events[-1]["data"]["actors"]] == ["ActorA1"]


def test_javdb_actor_stream_preserves_existing_subscription_state(client, account_user, monkeypatch, tmp_path):
    token = _login(client, username=account_user.username)
    _create_actor("Old Name", "ActorA1", alias_name="Old Alias", is_subscribed=True)

    class FakeProvider:
        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name="三上悠亞",
                    alias_names=["三上悠亞", actor_name, "鬼头桃菜"],
                    avatar_url=None,
                    gender=1,
                )
            ]

    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(
        ActorService,
        "_build_catalog_import_service",
        lambda: CatalogImportService(image_downloader=lambda url, target_path: target_path.write_bytes(b"img")),
    )

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert events[-1]["data"]["success"] is True
    assert events[-1]["data"]["actors"][0]["is_subscribed"] is True
    actor = Actor.get(Actor.javdb_id == "ActorA1")
    assert actor.alias_name == "三上悠亞 / 三上悠亚 / 鬼头桃菜 / Old Alias"
    assert actor.is_subscribed is True


def test_javdb_actor_stream_actor_not_found_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def __init__(self, host, proxy=None, actor_image_resolver=None):
            self.host = host
            self.proxy = proxy

        def search_actors(self, actor_name: str):
            raise MetadataNotFoundError("actor", actor_name)

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider(None))

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "not-exists"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [item["event"] for item in events] == ["search_started", "completed"]
    assert events[-1]["data"]["success"] is False
    assert events[-1]["data"]["reason"] == "actor_not_found"
    assert events[-1]["data"]["actors"] == []


def test_javdb_actor_stream_partial_success_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def __init__(self, host, proxy=None, actor_image_resolver=None):
            self.host = host
            self.proxy = proxy

        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=f"{actor_name}-1",
                    avatar_url="https://c0.jdbstatic.com/avatars/a1.jpg",
                    gender=1,
                ),
                JavdbMovieActorResource(
                    javdb_id="ActorA2",
                    name=f"{actor_name}-2",
                    avatar_url="https://c0.jdbstatic.com/avatars/a2.jpg",
                    gender=1,
                ),
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            if actor_resource.javdb_id == "ActorA2":
                raise ImageDownloadError("download_failed")
            return Actor.create(
                javdb_id=actor_resource.javdb_id,
                name=actor_resource.name,
                alias_name=actor_resource.name,
                gender=actor_resource.gender,
            )

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider(None))
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [item["event"] for item in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "image_download_finished",
        "image_download_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-2]["data"] == {
        "total": 2,
        "created_count": 1,
        "already_exists_count": 0,
        "failed_count": 1,
    }
    assert events[-1]["data"]["success"] is True
    assert [item["javdb_id"] for item in events[-1]["data"]["actors"]] == ["ActorA1"]
    assert events[-1]["data"]["failed_items"] == [
        {
            "javdb_id": "ActorA2",
            "reason": "image_download_failed",
            "detail": "download_failed",
        }
    ]


def test_javdb_actor_stream_all_failed_flow(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)

    class FakeProvider:
        def __init__(self, host, proxy=None, actor_image_resolver=None):
            self.host = host
            self.proxy = proxy

        def search_actors(self, actor_name: str):
            return [
                JavdbMovieActorResource(
                    javdb_id="ActorA1",
                    name=actor_name,
                    avatar_url="https://c0.jdbstatic.com/avatars/a.jpg",
                    gender=1,
                )
            ]

    class FakeCatalogImportService:
        def upsert_actor_from_javdb_resource(self, actor_resource):
            raise RuntimeError("db error")

    monkeypatch.setattr(ActorService, "_build_javdb_provider", lambda: FakeProvider(None))
    monkeypatch.setattr(ActorService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = client.post(
        "/actors/search/javdb/stream",
        json={"actor_name": "三上悠亚"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [item["event"] for item in events] == [
        "search_started",
        "actor_found",
        "upsert_started",
        "image_download_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1]["data"]["success"] is False
    assert events[-1]["data"]["reason"] == "internal_error"
    assert events[-1]["data"]["actors"] == []
    assert events[-1]["data"]["failed_items"] == [
        {
            "javdb_id": "ActorA1",
            "reason": "upsert_failed",
            "detail": "db error",
        }
    ]
