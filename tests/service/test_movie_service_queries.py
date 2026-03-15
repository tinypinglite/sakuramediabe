from datetime import datetime

import pytest

from src.api.exception.errors import ApiError
from src.common import build_signed_subtitle_url
from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    Image,
    Media,
    MediaPoint,
    MediaProgress,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
    Tag,
)
from src.schema.metadata.javdb import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
    JavdbMovieTagResource,
)
from src.schema.catalog.movies import MovieCollectionType, MovieListStatus
from src.service.catalog.catalog_import_service import CatalogImportService, ImageDownloadError
from src.service.catalog.movie_service import MovieService


def _capture_queries(test_db):
    queries: list[str] = []
    original_execute_sql = test_db.execute_sql

    def capture(sql, params=None, commit=None):
        queries.append(sql)
        return original_execute_sql(sql, params, commit)

    test_db.execute_sql = capture
    return queries, original_execute_sql


def _restore_queries(test_db, original_execute_sql):
    test_db.execute_sql = original_execute_sql


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


def test_movie_service_list_movies_uses_database_pagination_and_eager_loads_cover(
    app,
    test_db,
    build_signed_image_url,
):
    second_image = None
    for index in range(3):
        image = Image.create(
            origin=f"origin-{index}.jpg",
            small=f"small-{index}.jpg",
            medium=f"medium-{index}.jpg",
            large=f"large-{index}.jpg",
        )
        if index == 1:
            second_image = image
        _create_movie(
            f"ABC-{index:03d}",
            f"Movie{index}A",
            title=f"Movie {index}",
            cover_image=image,
            watched_count=index + 1,
            want_watch_count=index + 2,
            comment_count=index + 3,
            score_number=index + 4,
        )

    queries, original_execute_sql = _capture_queries(test_db)

    try:
        response = MovieService.list_movies(page=2, page_size=1)
    finally:
        _restore_queries(test_db, original_execute_sql)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "Movie1A",
                "movie_number": "ABC-001",
                "title": "Movie 1",
                "series_name": None,
                "cover_image": {
                    "id": second_image.id,
                    "origin": build_signed_image_url("origin-1.jpg"),
                    "small": build_signed_image_url("small-1.jpg"),
                    "medium": build_signed_image_url("medium-1.jpg"),
                    "large": build_signed_image_url("large-1.jpg"),
                },
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 2,
                "want_watch_count": 3,
                "comment_count": 4,
                "score_number": 5,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
            }
        ],
        "page": 2,
        "page_size": 1,
        "total": 3,
    }
    movie_selects = [sql for sql in queries if 'FROM "movie"' in sql]
    image_selects = [sql for sql in queries if 'FROM "image"' in sql]

    assert len(movie_selects) == 2
    assert any("LIMIT" in sql and "OFFSET" in sql for sql in movie_selects)
    assert image_selects == []


def test_movie_service_list_latest_movies_orders_by_latest_media_created_at_and_deduplicates(app):
    no_media_movie = _create_movie("ABC-000", "MovieA0", title="Movie 0")
    older_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    duplicated_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    tie_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")

    older_media = Media.create(movie=older_movie, path="/library/main/abc-001.mp4", valid=False)
    first_duplicate_media = Media.create(movie=duplicated_movie, path="/library/main/abc-002-a.mp4", valid=False)
    second_duplicate_media = Media.create(movie=duplicated_movie, path="/library/main/abc-002-b.mp4", valid=True)
    tie_media = Media.create(movie=tie_movie, path="/library/main/abc-003.mp4", valid=True)

    Media.update(created_at="2026-03-08 09:00:00").where(Media.id == older_media.id).execute()
    Media.update(created_at="2026-03-09 08:00:00").where(Media.id == first_duplicate_media.id).execute()
    Media.update(created_at="2026-03-10 08:00:00").where(Media.id == second_duplicate_media.id).execute()
    Media.update(created_at="2026-03-10 08:00:00").where(Media.id == tie_media.id).execute()

    response = MovieService.list_latest_movies(page=1, page_size=10)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA3",
                "movie_number": "ABC-003",
                "title": "Movie 3",
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
            },
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABC-002",
                "title": "Movie 2",
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
            },
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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
        "total": 3,
    }

    assert no_media_movie.movie_number not in [item.movie_number for item in response.items]


def test_movie_service_list_latest_movies_supports_pagination(app):
    first_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    second_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")

    first_media = Media.create(movie=first_movie, path="/library/main/abc-001.mp4", valid=True)
    second_media = Media.create(movie=second_movie, path="/library/main/abc-002.mp4", valid=True)

    Media.update(created_at="2026-03-08 09:00:00").where(Media.id == first_media.id).execute()
    Media.update(created_at="2026-03-09 09:00:00").where(Media.id == second_media.id).execute()

    response = MovieService.list_latest_movies(page=2, page_size=1)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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


def test_movie_service_list_subscribed_actor_latest_movies_filters_deduplicates_and_orders(app):
    subscribed_actor_a = _create_actor("三上悠亚", "ActorA1", is_subscribed=True)
    subscribed_actor_b = _create_actor("河北彩花", "ActorA2", is_subscribed=True)
    unsubscribed_actor = _create_actor("鬼头桃菜", "ActorA3", is_subscribed=False)

    latest_movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    older_movie = _create_movie(
        "ABC-002",
        "MovieA2",
        title="Movie 2",
        release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    no_release_date_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3", release_date=None)
    unsubscribed_actor_movie = _create_movie(
        "ABC-004",
        "MovieA4",
        title="Movie 4",
        release_date=datetime(2026, 3, 11, 9, 0, 0),
    )

    MovieActor.create(movie=latest_movie, actor=subscribed_actor_a)
    MovieActor.create(movie=latest_movie, actor=subscribed_actor_b)
    MovieActor.create(movie=older_movie, actor=subscribed_actor_a)
    MovieActor.create(movie=no_release_date_movie, actor=subscribed_actor_b)
    MovieActor.create(movie=unsubscribed_actor_movie, actor=unsubscribed_actor)
    Media.create(movie=older_movie, path="/library/main/abc-002.mp4", valid=True)

    response = MovieService.list_subscribed_actor_latest_movies(page=1, page_size=10)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
                "title": "Movie 1",
                "series_name": None,
                "cover_image": None,
                "release_date": "2026-03-10",
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
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABC-002",
                "title": "Movie 2",
                "series_name": None,
                "cover_image": None,
                "release_date": "2026-03-08",
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
                "javdb_id": "MovieA3",
                "movie_number": "ABC-003",
                "title": "Movie 3",
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
        "total": 3,
    }
    assert [item.movie_number for item in response.items] == ["ABC-001", "ABC-002", "ABC-003"]


def test_movie_service_list_subscribed_actor_latest_movies_supports_pagination(app):
    subscribed_actor = _create_actor("三上悠亚", "ActorA1", is_subscribed=True)
    first_movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2026, 3, 10, 9, 0, 0),
    )
    second_movie = _create_movie(
        "ABC-002",
        "MovieA2",
        title="Movie 2",
        release_date=datetime(2026, 3, 8, 9, 0, 0),
    )
    MovieActor.create(movie=first_movie, actor=subscribed_actor)
    MovieActor.create(movie=second_movie, actor=subscribed_actor)

    response = MovieService.list_subscribed_actor_latest_movies(page=2, page_size=1)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABC-002",
                "title": "Movie 2",
                "series_name": None,
                "cover_image": None,
                "release_date": "2026-03-08",
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
        ],
        "page": 2,
        "page_size": 1,
        "total": 2,
    }


def test_movie_service_get_movie_detail_eager_loads_cover_and_tags(
    app,
    test_db,
    build_signed_image_url,
):
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
    actor_image = Image.create(
        origin="actor-origin.jpg",
        small="actor-small.jpg",
        medium="actor-medium.jpg",
        large="actor-large.jpg",
    )
    actor_a = _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=actor_image,
    )
    actor_b = _create_actor("鬼头桃菜", "ActorB2", alias_name="三上悠亚 / 鬼头桃菜")
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
        summary="summary",
    )
    MovieActor.create(movie=movie, actor=actor_a)
    MovieActor.create(movie=movie, actor=actor_b)
    for name in ("剧情", "制服"):
        MovieTag.create(movie=movie, tag=Tag.create(name=name))
    recent_playlist = Playlist.create(
        kind=PLAYLIST_KIND_RECENTLY_PLAYED,
        name="最近播放",
        description="系统自动维护的最近播放影片列表",
    )
    custom_playlist = Playlist.create(name="我的收藏", description="Favorite")
    PlaylistMovie.create(playlist=custom_playlist, movie=movie)
    PlaylistMovie.create(playlist=recent_playlist, movie=movie)
    plot_image = Image.create(
        origin="plot-origin.jpg",
        small="plot-small.jpg",
        medium="plot-medium.jpg",
        large="plot-large.jpg",
    )
    MoviePlotImage.create(movie=movie, image=plot_image)

    queries, original_execute_sql = _capture_queries(test_db)

    try:
        response = MovieService.get_movie_detail("ABC-001")
    finally:
        _restore_queries(test_db, original_execute_sql)

    payload = response.model_dump(mode="json")
    assert payload["javdb_id"] == "MovieA1"
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
    assert payload["tags"] == [
        {"tag_id": 1, "name": "剧情"},
        {"tag_id": 2, "name": "制服"},
    ]
    assert payload["actors"] == [
        {
            "id": actor_a.id,
            "javdb_id": "ActorA1",
            "name": "三上悠亚",
            "alias_name": "三上悠亚 / 鬼头桃菜",
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
            "is_subscribed": False,
            "profile_image": None,
        },
    ]
    assert payload["plot_images"] == [
        {
            "id": plot_image.id,
            "origin": build_signed_image_url("plot-origin.jpg"),
            "small": build_signed_image_url("plot-small.jpg"),
            "medium": build_signed_image_url("plot-medium.jpg"),
            "large": build_signed_image_url("plot-large.jpg"),
        }
    ]
    assert payload["can_play"] is False
    assert payload["media_items"] == []
    assert payload["playlists"] == [
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

    image_joins = [sql for sql in queries if 'JOIN "image"' in sql]
    standalone_tag_selects = [sql for sql in queries if 'FROM "tag" AS "t1" WHERE' in sql]

    assert len(image_joins) == 3
    assert standalone_tag_selects == []


def test_movie_service_list_movies_filters_by_actor_id(app):
    actor = _create_actor("三上悠亚", "ActorA1")
    other_actor = _create_actor("鬼头桃菜", "ActorB2")
    actor_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    _create_movie("ABC-002", "MovieB2", title="Movie 2")
    MovieActor.create(movie=actor_movie, actor=actor)
    MovieActor.create(movie=actor_movie, actor=other_actor)

    response = MovieService.list_movies(actor_id=actor.id)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


def test_movie_service_list_movies_sets_can_play_from_valid_media(app):
    playable_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    not_playable_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    Media.create(
        movie=playable_movie,
        path="/library/main/abc-001.mp4",
        valid=True,
    )
    Media.create(
        movie=not_playable_movie,
        path="/library/main/abc-002.mp4",
        valid=False,
    )

    response = MovieService.list_movies(page=1, page_size=20)

    assert response.model_dump()["items"] == [
        {
            "javdb_id": "MovieA1",
            "movie_number": "ABC-001",
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
        },
        {
            "javdb_id": "MovieA2",
            "movie_number": "ABC-002",
            "title": "Movie 2",
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
    ]


def test_movie_service_list_movies_filters_by_subscribed_status(app):
    _create_movie("ABC-001", "MovieA1", title="Movie 1", is_subscribed=True)
    _create_movie("ABC-002", "MovieA2", title="Movie 2", is_subscribed=False)

    response = MovieService.list_movies(status=MovieListStatus.SUBSCRIBED)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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
                "is_subscribed": True,
                "can_play": False,
            }
        ],
        "page": 1,
        "page_size": 20,
        "total": 1,
    }


def test_movie_service_list_movies_filters_by_playable_status(app):
    playable_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    invalid_only_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    no_media_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    Media.create(movie=playable_movie, path="/library/main/abc-001.mp4", valid=True)
    Media.create(movie=invalid_only_movie, path="/library/main/abc-002.mp4", valid=False)

    response = MovieService.list_movies(status=MovieListStatus.PLAYABLE)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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

    assert no_media_movie.movie_number not in [item.movie_number for item in response.items]


def test_movie_service_list_movies_filters_by_single_collection_type(app):
    _create_movie("ABC-001", "MovieA1", title="Movie 1", is_collection=False)
    _create_movie("ABC-002", "MovieA2", title="Movie 2", is_collection=True)

    response = MovieService.list_movies(collection_type=MovieCollectionType.SINGLE)

    assert [item.movie_number for item in response.items] == ["ABC-001"]


def test_movie_service_list_movies_filters_by_actor_and_playable_status(app):
    actor = _create_actor("三上悠亚", "ActorA1")
    matched_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    actor_without_playable_media = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    other_actor_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    MovieActor.create(movie=matched_movie, actor=actor)
    MovieActor.create(movie=actor_without_playable_media, actor=actor)
    Media.create(movie=matched_movie, path="/library/main/abc-001.mp4", valid=True)
    Media.create(movie=actor_without_playable_media, path="/library/main/abc-002.mp4", valid=False)

    other_actor = _create_actor("鬼头桃菜", "ActorB2")
    MovieActor.create(movie=other_actor_movie, actor=other_actor)
    Media.create(movie=other_actor_movie, path="/library/main/abc-003.mp4", valid=True)

    response = MovieService.list_movies(actor_id=actor.id, status=MovieListStatus.PLAYABLE)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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


def test_movie_service_list_movies_keeps_default_movie_number_sort(app):
    _create_movie("ABC-003", "MovieA3", title="Movie 3")
    _create_movie("ABC-001", "MovieA1", title="Movie 1")
    _create_movie("ABC-002", "MovieA2", title="Movie 2")

    response = MovieService.list_movies()

    assert [item.movie_number for item in response.items] == ["ABC-001", "ABC-002", "ABC-003"]


def test_movie_service_list_movies_supports_release_date_sort_with_nulls_last(app):
    _create_movie("ABC-001", "MovieA1", title="Movie 1", release_date=datetime(2026, 3, 8, 9, 0, 0))
    _create_movie("ABC-002", "MovieA2", title="Movie 2", release_date=None)
    _create_movie("ABC-003", "MovieA3", title="Movie 3", release_date=datetime(2026, 3, 10, 9, 0, 0))

    response = MovieService.list_movies(sort="release_date:desc")

    assert [item.movie_number for item in response.items] == ["ABC-003", "ABC-001", "ABC-002"]


def test_movie_service_list_movies_supports_added_at_sort_desc(app):
    _create_movie("ABC-001", "MovieA1", title="Movie 1")
    _create_movie("ABC-002", "MovieA2", title="Movie 2")
    _create_movie("ABC-003", "MovieA3", title="Movie 3")

    response = MovieService.list_movies(sort="added_at:desc")

    assert [item.movie_number for item in response.items] == ["ABC-003", "ABC-002", "ABC-001"]


@pytest.mark.parametrize(
    ("sort", "field_name"),
    [
        ("comment_count:desc", "comment_count"),
        ("score_number:desc", "score_number"),
        ("want_watch_count:desc", "want_watch_count"),
        ("heat:desc", "heat"),
    ],
)
def test_movie_service_list_movies_supports_metric_sorts_with_id_tie_break(app, sort: str, field_name: str):
    _create_movie("ABC-001", "MovieA1", title="Movie 1", **{field_name: 10})
    _create_movie("ABC-002", "MovieA2", title="Movie 2", **{field_name: 10})
    _create_movie("ABC-003", "MovieA3", title="Movie 3", **{field_name: 5})

    response = MovieService.list_movies(sort=sort)

    assert [item.movie_number for item in response.items] == ["ABC-002", "ABC-001", "ABC-003"]


def test_movie_service_list_movies_supports_subscribed_at_sort_with_nulls_last(app):
    _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 8, 9, 0, 0),
    )
    _create_movie("ABC-002", "MovieA2", title="Movie 2", is_subscribed=False, subscribed_at=None)
    _create_movie(
        "ABC-003",
        "MovieA3",
        title="Movie 3",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )

    response = MovieService.list_movies(sort="subscribed_at:desc")

    assert [item.movie_number for item in response.items] == ["ABC-003", "ABC-001", "ABC-002"]


def test_movie_service_set_subscription_sets_subscribed_at_for_unsubscribed_movie(app):
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1", is_subscribed=False, subscribed_at=None)

    MovieService.set_subscription("ABC-001", True)

    movie = Movie.get_by_id(movie.id)
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None


def test_movie_service_set_subscription_keeps_existing_subscribed_at(app):
    original_subscribed_at = datetime(2026, 3, 8, 9, 0, 0)
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=original_subscribed_at,
    )

    MovieService.set_subscription("ABC-001", True)

    movie = Movie.get_by_id(movie.id)
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_subscribed_at


def test_movie_service_set_subscription_returns_not_found_for_missing_movie(app):
    with pytest.raises(ApiError) as exc_info:
        MovieService.set_subscription("ABC-404", True)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "movie_not_found"
    assert exc_info.value.details == {"movie_number": "ABC-404"}


def test_movie_service_unsubscribe_movie_without_media_clears_subscription(app):
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )

    MovieService.unsubscribe_movie("ABC-001")

    movie = Movie.get_by_id(movie.id)
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None


def test_movie_service_unsubscribe_movie_rejects_when_media_exists_without_delete_flag(app):
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    media = Media.create(movie=movie, path="/library/main/abc-001.mp4", valid=True)

    with pytest.raises(ApiError) as exc_info:
        MovieService.unsubscribe_movie("ABC-001")

    movie = Movie.get_by_id(movie.id)
    media = Media.get_by_id(media.id)
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "movie_subscription_has_media"
    assert exc_info.value.details == {
        "movie_number": "ABC-001",
        "media_count": 1,
        "delete_media_required": True,
    }
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None
    assert media.valid is True


def test_movie_service_unsubscribe_movie_with_delete_media_removes_files_and_invalidates_all_media(
    app,
    tmp_path,
):
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    first_path = tmp_path / "abc-001-main.mp4"
    second_path = tmp_path / "abc-001-backup.mp4"
    first_path.write_bytes(b"main")
    second_path.write_bytes(b"backup")
    first_media = Media.create(movie=movie, path=str(first_path), valid=True)
    second_media = Media.create(movie=movie, path=str(second_path), valid=False)

    MovieService.unsubscribe_movie("ABC-001", delete_media=True)

    movie = Movie.get_by_id(movie.id)
    first_media = Media.get_by_id(first_media.id)
    second_media = Media.get_by_id(second_media.id)
    assert first_path.exists() is False
    assert second_path.exists() is False
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None
    assert first_media.valid is False
    assert second_media.valid is False


def test_movie_service_unsubscribe_movie_with_delete_media_ignores_missing_files(app, tmp_path):
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 10, 9, 0, 0),
    )
    existing_path = tmp_path / "abc-001-main.mp4"
    missing_path = tmp_path / "abc-001-missing.mp4"
    existing_path.write_bytes(b"main")
    first_media = Media.create(movie=movie, path=str(existing_path), valid=True)
    second_media = Media.create(movie=movie, path=str(missing_path), valid=True)

    MovieService.unsubscribe_movie("ABC-001", delete_media=True)

    movie = Movie.get_by_id(movie.id)
    first_media = Media.get_by_id(first_media.id)
    second_media = Media.get_by_id(second_media.id)
    assert existing_path.exists() is False
    assert movie.is_subscribed is False
    assert movie.subscribed_at is None
    assert first_media.valid is False
    assert second_media.valid is False


def test_movie_service_list_movies_rejects_invalid_sort(app):
    with pytest.raises(ApiError) as exc_info:
        MovieService.list_movies(sort="id:desc")

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_movie_filter"
    assert exc_info.value.details == {"sort": "id:desc"}


def test_movie_service_get_movie_detail_returns_media_items_with_progress_and_points(
    app,
    tmp_path,
    build_signed_media_url,
):
    movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    version_dir = tmp_path / "ABC-001" / "1730000000000"
    version_dir.mkdir(parents=True)
    playable_path = version_dir / "ABC-001.mp4"
    playable_path.write_bytes(b"main")
    imported_subtitle = version_dir / "ABC-001.srt"
    imported_subtitle.write_text("imported", encoding="utf-8")
    manual_subtitle = version_dir / "ABC-001.zh.srt"
    manual_subtitle.write_text("manual", encoding="utf-8")
    playable_media = Media.create(
        movie=movie,
        path=str(playable_path),
        storage_mode="hardlink",
        resolution="1920x1080",
        file_size_bytes=100,
        duration_seconds=7200,
        special_tags="普通",
        valid=True,
    )
    second_media = Media.create(
        movie=movie,
        path=str(tmp_path / "ABC-001" / "1730000000001" / "ABC-001-backup.mp4"),
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
    first_point = MediaPoint.create(media=playable_media, offset_seconds=120)
    second_point = MediaPoint.create(media=playable_media, offset_seconds=360)
    backup_point = MediaPoint.create(media=second_media, offset_seconds=90)

    response = MovieService.get_movie_detail("ABC-001")

    payload = response.model_dump(mode="json")
    assert payload["can_play"] is True
    assert payload["playlists"] == []
    assert payload["media_items"] == [
        {
            "media_id": playable_media.id,
            "library_id": None,
            "play_url": build_signed_media_url(playable_media.id),
            "storage_mode": "hardlink",
            "resolution": "1920x1080",
            "file_size_bytes": 100,
            "duration_seconds": 7200,
            "special_tags": "普通",
            "valid": True,
            "progress": {
                "last_position_seconds": 600,
                "last_watched_at": "2026-03-08T09:30:00",
            },
            "points": [
                {"point_id": first_point.id, "offset_seconds": 120},
                {"point_id": second_point.id, "offset_seconds": 360},
            ],
            "subtitles": [
                {
                    "file_name": "ABC-001.srt",
                    "url": build_signed_subtitle_url(playable_media.id, "ABC-001.srt"),
                },
                {
                    "file_name": "ABC-001.zh.srt",
                    "url": build_signed_subtitle_url(playable_media.id, "ABC-001.zh.srt"),
                },
            ],
        },
        {
            "media_id": second_media.id,
            "library_id": None,
            "play_url": build_signed_media_url(second_media.id),
            "storage_mode": "copy",
            "resolution": "1280x720",
            "file_size_bytes": 50,
            "duration_seconds": 3600,
            "special_tags": "无码",
            "valid": False,
            "progress": None,
            "points": [
                {"point_id": backup_point.id, "offset_seconds": 90},
            ],
            "subtitles": [],
        },
    ]


def test_movie_service_get_movie_detail_returns_empty_subtitles_when_media_file_missing(
    app,
    tmp_path,
    build_signed_media_url,
):
    movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    missing_path = tmp_path / "ABC-002" / "1730000000000" / "ABC-002.mp4"
    missing_path.parent.mkdir(parents=True)
    (missing_path.parent / "ABC-002.srt").write_text("manual", encoding="utf-8")
    media = Media.create(movie=movie, path=str(missing_path), valid=True)

    response = MovieService.get_movie_detail("ABC-002")

    payload = response.model_dump(mode="json")
    assert payload["media_items"] == [
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
            "subtitles": [],
        }
    ]


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


def test_parse_movie_number_query_returns_parsed_movie_number():
    response = MovieService.parse_movie_number_query("/downloads/abp123.mp4")

    assert response.model_dump() == {
        "query": "/downloads/abp123.mp4",
        "parsed": True,
        "movie_number": "ABP-123",
        "reason": None,
    }


def test_parse_movie_number_query_returns_not_found_when_parse_failed():
    response = MovieService.parse_movie_number_query("no movie number here")

    assert response.model_dump() == {
        "query": "no movie number here",
        "parsed": False,
        "movie_number": None,
        "reason": "movie_number_not_found",
    }


def test_search_local_movies_normalizes_movie_number(app):
    _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1")

    response = MovieService.search_local_movies("fc2-123456")

    assert [item.model_dump() for item in response] == [
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


def test_search_local_movies_returns_empty_when_not_found(app):
    _create_movie("ABP-123", "MovieA1", title="Movie 1")

    response = MovieService.search_local_movies("SSNI-404")

    assert response == []


def test_stream_search_movie_uses_catalog_import_service(app, monkeypatch):
    called = {"upsert": 0}

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            called["upsert"] += 1
            return Movie.create(
                javdb_id=detail.javdb_id,
                movie_number=detail.movie_number,
                title=detail.title,
            )

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(MovieService.stream_search_and_upsert_movie_from_javdb("abp123"))

    assert called["upsert"] == 1
    assert [event for event, _ in events] == [
        "search_started",
        "movie_found",
        "upsert_started",
        "upsert_finished",
        "completed",
    ]
    assert events[1][1]["total"] == 1
    assert events[-2][1] == {
        "total": 1,
        "created_count": 1,
        "already_exists_count": 0,
        "failed_count": 0,
    }
    assert events[-1][1]["success"] is True
    assert len(events[-1][1]["movies"]) == 1
    assert events[-1][1]["failed_items"] == []


def test_stream_search_movie_returns_existing_subscription_state_after_upsert(app, monkeypatch, tmp_path):
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

    events = list(MovieService.stream_search_and_upsert_movie_from_javdb("ABP-123"))

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_timestamp
    assert events[-1][1]["success"] is True
    assert events[-1][1]["movies"][0]["is_subscribed"] is True


def test_stream_search_movie_returns_not_found_when_provider_misses(app, monkeypatch):
    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    events = list(MovieService.stream_search_and_upsert_movie_from_javdb("unknown"))

    assert [event for event, _ in events] == [
        "search_started",
        "completed",
    ]
    assert events[-1][1]["reason"] == "movie_not_found"


def test_stream_search_movie_maps_image_download_failure(app, monkeypatch):
    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            raise ImageDownloadError("download_failed")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(MovieService.stream_search_and_upsert_movie_from_javdb("ABP-123"))

    assert [event for event, _ in events] == [
        "search_started",
        "movie_found",
        "upsert_started",
        "upsert_finished",
        "completed",
    ]
    assert events[-1][1]["success"] is False
    assert events[-1][1]["reason"] == "internal_error"
    assert events[-1][1]["movies"] == []
    assert events[-1][1]["failed_items"] == [
        {
            "movie_number": "ABP-123",
            "reason": "image_download_failed",
            "detail": "download_failed",
        }
    ]
