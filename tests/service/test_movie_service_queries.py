from datetime import datetime

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from sakuramedia_metadata_providers.exceptions import MissavThumbnailRequestError
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
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
    MovieSeries,
    MovieTag,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Playlist,
    PlaylistMovie,
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
from src.schema.catalog.movies import (
    MissavThumbnailItemResource,
    MissavThumbnailResource,
    MovieCollectionMarkType,
    MovieCollectionType,
    MovieListStatus,
    MovieReviewSort,
    MovieSpecialTagFilter,
)
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


def test_movie_service_list_movies_by_series_uses_local_series_id(app):
    series_a = MovieSeries.create(name="A 系列")
    series_b = MovieSeries.create(name="B 系列")
    _create_movie("ABP-001", "javdb-a1", series=series_a)
    _create_movie("ABP-002", "javdb-a2", series=series_a)
    _create_movie("ABP-003", "javdb-b1", series=series_b)
    _create_movie("ABP-004", "javdb-none")

    response = MovieService.list_movies_by_series(series_id=series_a.id)

    assert response.total == 2
    assert {item.movie_number for item in response.items} == {"ABP-001", "ABP-002"}
    assert all(item.series_id == series_a.id for item in response.items)


def test_movie_service_list_movies_by_series_returns_empty_for_missing_series_id(app):
    series = MovieSeries.create(name="A 系列")
    _create_movie("ABP-001", "javdb-a1", series=series)

    response = MovieService.list_movies_by_series(series_id=series.id + 1000)

    assert response.total == 0
    assert response.items == []


def test_movie_service_list_movies_uses_database_pagination_and_eager_loads_cover(
    app,
    test_db,
    build_signed_image_url,
):
    second_image = None
    second_thin_image = None
    for index in range(3):
        image = Image.create(
            origin=f"origin-{index}.jpg",
            small=f"small-{index}.jpg",
            medium=f"medium-{index}.jpg",
            large=f"large-{index}.jpg",
        )
        thin_image = Image.create(
            origin=f"thin-origin-{index}.jpg",
            small=f"thin-small-{index}.jpg",
            medium=f"thin-medium-{index}.jpg",
            large=f"thin-large-{index}.jpg",
        )
        if index == 1:
            second_image = image
            second_thin_image = thin_image
        _create_movie(
            f"ABC-{index:03d}",
            f"Movie{index}A",
            title=f"Movie {index}",
            title_zh=f"中文影片 {index}",
            cover_image=image,
            thin_cover_image=thin_image,
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
                "title_zh": "中文影片 1",
                "series_id": None,
                "series_name": None,
                "cover_image": {
                    "id": second_image.id,
                    "origin": build_signed_image_url("origin-1.jpg"),
                    "small": build_signed_image_url("small-1.jpg"),
                    "medium": build_signed_image_url("medium-1.jpg"),
                    "large": build_signed_image_url("large-1.jpg"),
                },
                "thin_cover_image": {
                    "id": second_thin_image.id,
                    "origin": build_signed_image_url("thin-origin-1.jpg"),
                    "small": build_signed_image_url("thin-small-1.jpg"),
                    "medium": build_signed_image_url("thin-medium-1.jpg"),
                    "large": build_signed_image_url("thin-large-1.jpg"),
                },
                "release_date": None,
                "duration_minutes": 0,
                "score": 0.0,
                "watched_count": 2,
                "want_watch_count": 3,
                "comment_count": 4,
                "score_number": 5,
                "heat": 0,
                "is_collection": False,
                "is_subscribed": False,
                "can_play": False,
                "is_4k": False,
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
            },
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABC-002",
                "title": "Movie 2",
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
            },
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
                "can_play": False,
                "is_4k": False,
            },
        ],
        "page": 1,
        "page_size": 10,
        "total": 3,
    }

    assert no_media_movie.movie_number not in [item.movie_number for item in response.items]


def test_movie_service_list_movies_filters_by_director_and_maker(app):
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

    response = MovieService.list_movies(
        director_name="嵐山みちる",
        maker_name="S1 NO.1 STYLE",
    )

    assert response.total == 1
    assert [item.movie_number for item in response.items] == ["ABP-120"]


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
    collection_movie = _create_movie(
        "ABC-005",
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
    Media.create(movie=older_movie, path="/library/main/abc-002.mp4", valid=True)

    response = MovieService.list_subscribed_actor_latest_movies(page=1, page_size=10)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA1",
                "movie_number": "ABC-001",
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
                "movie_number": "ABC-002",
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
                "movie_number": "ABC-003",
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
    collection_movie = _create_movie(
        "ABC-003",
        "MovieA3",
        title="Movie 3",
        release_date=datetime(2026, 3, 11, 9, 0, 0),
        is_collection=True,
    )
    MovieActor.create(movie=first_movie, actor=subscribed_actor)
    MovieActor.create(movie=second_movie, actor=subscribed_actor)
    MovieActor.create(movie=collection_movie, actor=subscribed_actor)

    response = MovieService.list_subscribed_actor_latest_movies(page=2, page_size=1)

    assert response.model_dump() == {
        "items": [
            {
                "javdb_id": "MovieA2",
                "movie_number": "ABC-002",
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
        gender=1,
        profile_image=actor_image,
    )
    actor_b = _create_actor("鬼头桃菜", "ActorB2", alias_name="三上悠亚 / 鬼头桃菜", gender=2)
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
        summary="summary",
        maker_name="S1 NO.1 STYLE",
        director_name="嵐山みちる",
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
    assert payload["desc"] == ""
    assert payload["maker_name"] == "S1 NO.1 STYLE"
    assert payload["director_name"] == "嵐山みちる"
    assert payload["heat"] == 0
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
    assert payload["is_4k"] is False
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
        },
        {
            "javdb_id": "MovieA2",
            "movie_number": "ABC-002",
            "title": "Movie 2",
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
    ]


def test_movie_service_list_movies_sets_is_4k_from_valid_media_only(app):
    valid_4k_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    invalid_4k_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    normal_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    Media.create(
        movie=valid_4k_movie,
        path="/library/main/abc-001.mp4",
        valid=True,
        special_tags="4K",
    )
    Media.create(
        movie=invalid_4k_movie,
        path="/library/main/abc-002.mp4",
        valid=False,
        special_tags="4K",
    )
    Media.create(
        movie=normal_movie,
        path="/library/main/abc-003.mp4",
        valid=True,
        special_tags="普通",
    )

    response = MovieService.list_movies(page=1, page_size=20)

    assert [item.model_dump()["is_4k"] for item in response.items] == [True, False, False]


@pytest.mark.parametrize(
    ("special_tag", "matched_tags"),
    [
        (MovieSpecialTagFilter.FOUR_K, "4K"),
        (MovieSpecialTagFilter.UNCENSORED, "无码"),
        (MovieSpecialTagFilter.VR, "VR"),
    ],
)
def test_movie_service_list_movies_filters_by_special_tag_using_valid_media_only(
    app,
    special_tag: MovieSpecialTagFilter,
    matched_tags: str,
):
    matched_movie = _create_movie("ABC-001", "MovieA1", title="Movie 1")
    invalid_matched_movie = _create_movie("ABC-002", "MovieA2", title="Movie 2")
    other_movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    Media.create(
        movie=matched_movie,
        path="/library/main/abc-001.mp4",
        valid=True,
        special_tags=matched_tags,
    )
    Media.create(
        movie=invalid_matched_movie,
        path="/library/main/abc-002.mp4",
        valid=False,
        special_tags=matched_tags,
    )
    Media.create(
        movie=other_movie,
        path="/library/main/abc-003.mp4",
        valid=True,
        special_tags="普通",
    )

    response = MovieService.list_movies(special_tag=special_tag)

    assert [item.movie_number for item in response.items] == ["ABC-001"]


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
                "is_subscribed": True,
                "can_play": False,
                "is_4k": False,
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


def test_movie_service_mark_movie_collection_type_marks_matched_movies_with_override(app):
    first_movie = _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1", is_collection=True)
    second_movie = _create_movie("ABP-123", "MovieA2", title="Movie 2", is_collection=True)

    result = MovieService.mark_movie_collection_type(
        movie_numbers=["fc2-123456", "ABP-123", "ABP-404", "ABP-123"],
        collection_type=MovieCollectionMarkType.SINGLE,
    )

    first_movie = Movie.get_by_id(first_movie.id)
    second_movie = Movie.get_by_id(second_movie.id)
    assert result.model_dump() == {"requested_count": 4, "updated_count": 2}
    assert first_movie.is_collection is False
    assert first_movie.is_collection_overridden is True
    assert second_movie.is_collection is False
    assert second_movie.is_collection_overridden is True


def test_movie_service_mark_movie_collection_type_returns_zero_when_all_numbers_do_not_match(app):
    _create_movie("ABP-123", "MovieA1", title="Movie 1", is_collection=False)

    result = MovieService.mark_movie_collection_type(
        movie_numbers=["XYZ-404", "XYZ-405"],
        collection_type=MovieCollectionMarkType.COLLECTION,
    )

    assert result.model_dump() == {"requested_count": 2, "updated_count": 0}


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


def test_movie_service_unsubscribe_movie_rejects_when_media_exists(app):
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
    }
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None
    assert media.valid is True


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
    build_signed_image_url,
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
    playable_video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 7200},
        "video": {"codec_name": "h264", "profile": "Main"},
        "audio": {"codec_name": "aac"},
        "subtitles": [],
    }
    backup_video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 3600},
        "video": {"codec_name": "hevc", "profile": "Main 10"},
        "audio": None,
        "subtitles": [],
    }
    playable_media = Media.create(
        movie=movie,
        path=str(playable_path),
        storage_mode="hardlink",
        resolution="1920x1080",
        file_size_bytes=100,
        duration_seconds=7200,
        video_info=playable_video_info,
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
        video_info=backup_video_info,
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
    backup_thumbnail = _create_thumbnail(second_media, offset_seconds=90, suffix="backup")
    first_point = MediaPoint.create(media=playable_media, thumbnail=first_thumbnail, offset_seconds=120)
    second_point = MediaPoint.create(media=playable_media, thumbnail=second_thumbnail, offset_seconds=360)
    backup_point = MediaPoint.create(media=second_media, thumbnail=backup_thumbnail, offset_seconds=90)

    response = MovieService.get_movie_detail("ABC-001")

    payload = response.model_dump(mode="json")
    assert payload["can_play"] is True
    assert payload["is_4k"] is False
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
            "video_info": playable_video_info,
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
            "media_id": second_media.id,
            "library_id": None,
            "play_url": build_signed_media_url(second_media.id),
            "storage_mode": "copy",
            "resolution": "1280x720",
            "file_size_bytes": 50,
            "duration_seconds": 3600,
            "video_info": backup_video_info,
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


def test_movie_service_get_movie_detail_sets_is_4k_from_valid_media_only(app):
    movie = _create_movie("ABC-003", "MovieA3", title="Movie 3")
    Media.create(
        movie=movie,
        path="/library/main/abc-003-main.mp4",
        valid=True,
        special_tags="4K",
    )
    Media.create(
        movie=movie,
        path="/library/main/abc-003-backup.mp4",
        valid=False,
        special_tags="普通",
    )

    response = MovieService.get_movie_detail("ABC-003")

    assert response.is_4k is True


def test_movie_service_get_movie_detail_keeps_media_payload_when_media_file_missing(
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
            "video_info": None,
            "special_tags": "普通",
            "valid": True,
            "progress": None,
            "points": [],
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


def test_search_local_movies_returns_empty_when_not_found(app):
    _create_movie("ABP-123", "MovieA1", title="Movie 1")

    response = MovieService.search_local_movies("SSNI-404")

    assert response == []


def test_get_movie_collection_status_returns_local_movie_state(app):
    _create_movie("FC2-PPV-123456", "MovieA1", title="Movie 1", is_collection=True)

    response = MovieService.get_movie_collection_status("fc2-123456")

    assert response.model_dump() == {
        "movie_number": "FC2-PPV-123456",
        "is_collection": True,
    }


def test_get_movie_collection_status_returns_not_found_when_movie_missing(app):
    _create_movie("ABP-123", "MovieA1", title="Movie 1", is_collection=False)

    with pytest.raises(ApiError) as exc_info:
        MovieService.get_movie_collection_status("SSNI-404")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "movie_not_found"
    assert exc_info.value.details == {"movie_number": "SSNI-404"}


def test_get_movie_reviews_reads_javdb_reviews_by_movie_number(app, monkeypatch):
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
                    score=5,
                    content="非常棒",
                    created_at="2026-03-10T08:00:00Z",
                    username="reviewer",
                    like_count=7,
                    watch_count=18,
                )
            ]

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    reviews = MovieService.get_movie_reviews(
        "ABP-123",
        page=2,
        page_size=5,
        sort=MovieReviewSort.HOTLY,
    )

    assert [review.model_dump(mode="json") for review in reviews] == [
        {
            "id": 1,
            "score": 5,
            "content": "非常棒",
                "created_at": "2026-03-10T08:00:00Z",
            "username": "reviewer",
            "like_count": 7,
            "watch_count": 18,
            "movie": None,
        }
    ]
    assert captured == {
        "javdb_id": "javdb-ABP-123",
        "page": 2,
        "limit": 5,
        "sort_by": "hotly",
    }


def test_get_movie_reviews_returns_not_found_when_movie_missing(app):
    with pytest.raises(ApiError) as exc_info:
        MovieService.get_movie_reviews("ABP-404")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "movie_not_found"
    assert exc_info.value.details == {"movie_number": "ABP-404"}


def test_get_movie_reviews_maps_metadata_not_found_error_to_movie_not_found(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_reviews_by_javdb_id(
            self,
            javdb_id: str,
            page: int = 1,
            limit: int = 20,
            sort_by: str = "recently",
        ):
            raise MetadataNotFoundError("movie", javdb_id)

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    with pytest.raises(ApiError) as exc_info:
        MovieService.get_movie_reviews("ABP-123")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "movie_not_found"
    assert exc_info.value.details == {
        "movie_number": "ABP-123",
        "javdb_id": "javdb-ABP-123",
    }


def test_get_movie_reviews_maps_metadata_request_error_to_502(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_reviews_by_javdb_id(
            self,
            javdb_id: str,
            page: int = 1,
            limit: int = 20,
            sort_by: str = "recently",
        ):
            raise MetadataRequestError("GET", "https://example.com/reviews", "bad gateway")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    with pytest.raises(ApiError) as exc_info:
        MovieService.get_movie_reviews("ABP-123")

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "movie_review_fetch_failed"
    assert exc_info.value.details["movie_number"] == "ABP-123"
    assert exc_info.value.details["javdb_id"] == "javdb-ABP-123"


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


def test_stream_import_series_movies_deduplicates_and_skips_existing(app, monkeypatch):
    local_series = MovieSeries.create(name="A 系列")
    _create_movie("ABP-001", "javdb-existing", title="Existing", series_name="A 系列")
    fetched_detail_ids = []

    class FakeProvider:
        def search_series(self, series_name: str):
            return [JavdbSeriesResource(javdb_id="series-1", javdb_type=3, name=series_name, videos_count=3)]

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
            fetched_detail_ids.append(javdb_id)
            return _build_detail("ABP-002")

    class FakeCatalogImportService:
        def upsert_movie_from_javdb_detail(self, detail):
            return Movie.create(
                javdb_id=detail.javdb_id,
                movie_number=detail.movie_number,
                title=detail.title,
                series_name=detail.series_name,
            )

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    events = list(MovieService.stream_import_series_movies_from_javdb(local_series.id))

    assert [event for event, _ in events] == [
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
    assert fetched_detail_ids == ["javdb-new"]
    assert events[3][1]["total"] == 2
    assert events[-2][1] == {
        "total": 2,
        "created_count": 1,
        "already_exists_count": 1,
        "failed_count": 0,
    }
    assert events[-1][1]["success"] is True
    assert [item["movie_number"] for item in events[-1][1]["movies"]] == ["ABP-002"]


def test_refresh_movie_metadata_matches_normalized_movie_number_and_returns_updated_detail(app, monkeypatch):
    movie = _create_movie(
        "FC2-PPV-123456",
        "javdb-FC2-PPV-123456",
        title="old-title",
        desc="keep-desc",
        desc_zh="keep-desc-zh",
    )
    captured: dict[str, object] = {}

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            captured["provider_movie_number"] = movie_number
            detail = _build_detail("FC2-PPV-123456")
            detail.javdb_id = "javdb-FC2-PPV-123456-remote"
            detail.title = "new-title"
            detail.extra = {"remote": "payload"}
            return detail

    class FakeCatalogImportService:
        def refresh_movie_metadata_strict(self, target_movie, detail):
            captured["refreshed_movie_id"] = target_movie.id
            target_movie.javdb_id = detail.javdb_id
            target_movie.title = detail.title
            target_movie.summary = detail.summary
            target_movie.extra = detail.extra
            target_movie.save()
            return target_movie

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    response = MovieService.refresh_movie_metadata("fc2-123456")

    assert captured == {
        "provider_movie_number": "FC2-123456",
        "refreshed_movie_id": movie.id,
    }
    assert response.movie_number == "FC2-PPV-123456"
    assert response.javdb_id == "javdb-FC2-PPV-123456-remote"
    assert response.title == "new-title"
    assert Movie.get_by_id(movie.id).extra == {"remote": "payload"}
    assert response.desc == "keep-desc"
    assert response.desc_zh == "keep-desc-zh"


def test_refresh_movie_metadata_returns_remote_not_found_when_provider_misses(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    with pytest.raises(ApiError) as exc_info:
        MovieService.refresh_movie_metadata("ABP-123")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "movie_metadata_not_found"
    assert exc_info.value.details == {
        "movie_number": "ABP-123",
        "normalized_movie_number": "ABP-123",
    }


def test_refresh_movie_metadata_rejects_conflicting_remote_movie_number(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail("SSNI-888")
            detail.movie_number = "SSNI-888"
            return detail

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    with pytest.raises(ApiError) as exc_info:
        MovieService.refresh_movie_metadata("ABP-123")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "movie_metadata_number_conflict"
    assert exc_info.value.details == {
        "movie_number": "ABP-123",
        "normalized_movie_number": "ABP-123",
        "remote_movie_number": "SSNI-888",
        "remote_normalized_movie_number": "SSNI-888",
    }


def test_refresh_movie_metadata_rejects_conflicting_remote_javdb_id(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")
    _create_movie("ABP-456", "javdb-ABP-456-remote", title="Movie 2")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            detail = _build_detail("ABP-123")
            detail.javdb_id = "javdb-ABP-456-remote"
            return detail

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())

    with pytest.raises(ApiError) as exc_info:
        MovieService.refresh_movie_metadata("ABP-123")

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "movie_metadata_javdb_id_conflict"
    assert exc_info.value.details == {
        "movie_number": "ABP-123",
        "normalized_movie_number": "ABP-123",
        "current_javdb_id": "javdb-ABP-123",
        "remote_javdb_id": "javdb-ABP-456-remote",
        "conflicting_movie_number": "ABP-456",
    }


def test_refresh_movie_metadata_maps_refresh_failure_to_502(app, monkeypatch):
    _create_movie("ABP-123", "javdb-ABP-123", title="Movie 1")

    class FakeProvider:
        def get_movie_by_number(self, movie_number: str):
            return _build_detail(movie_number)

    class FakeCatalogImportService:
        def refresh_movie_metadata_strict(self, movie, detail):
            raise ImageDownloadError("image refresh failed")

    monkeypatch.setattr(MovieService, "_build_javdb_provider", lambda: FakeProvider())
    monkeypatch.setattr(MovieService, "_build_catalog_import_service", lambda: FakeCatalogImportService())

    with pytest.raises(ApiError) as exc_info:
        MovieService.refresh_movie_metadata("ABP-123")

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "movie_metadata_refresh_failed"
    assert exc_info.value.details == {
        "movie_number": "ABP-123",
        "normalized_movie_number": "ABP-123",
        "detail": "image refresh failed",
    }


def test_stream_missav_thumbnails_emits_progress_and_completed_result(monkeypatch):
    class FakeService:
        def get_movie_thumbnails(self, movie_number: str, *, refresh: bool = False, progress_callback=None):
            if progress_callback is not None:
                progress_callback(
                    "manifest_resolved",
                    {"movie_number": movie_number, "sprite_total": 2, "thumbnail_total": 3},
                )
                progress_callback("download_started", {"total": 2})
                progress_callback("download_progress", {"completed": 1, "total": 2})
                progress_callback("download_finished", {"completed": 2, "total": 2})
                progress_callback("slice_started", {"total": 3})
                progress_callback("slice_progress", {"completed": 3, "total": 3})
                progress_callback("slice_finished", {"completed": 3, "total": 3})
            return MissavThumbnailResource(
                movie_number=movie_number,
                source="missav",
                total=3,
                items=[
                    MissavThumbnailItemResource(index=0, url="/files/images/movies/SSNI-888/missav-seek/frames/0.jpg"),
                    MissavThumbnailItemResource(index=1, url="/files/images/movies/SSNI-888/missav-seek/frames/1.jpg"),
                    MissavThumbnailItemResource(index=2, url="/files/images/movies/SSNI-888/missav-seek/frames/2.jpg"),
                ],
            )

    monkeypatch.setattr(MovieService, "_build_missav_thumbnail_service", lambda: FakeService())

    events = list(MovieService.stream_missav_thumbnails("SSNI-888", refresh=True))

    assert [event for event, _ in events] == [
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
    assert events[0][1] == {"movie_number": "SSNI-888", "refresh": True}
    assert events[-1][1] == {
        "success": True,
        "result": {
            "movie_number": "SSNI-888",
            "source": "missav",
            "total": 3,
            "items": [
                {"index": 0, "url": "/files/images/movies/SSNI-888/missav-seek/frames/0.jpg"},
                {"index": 1, "url": "/files/images/movies/SSNI-888/missav-seek/frames/1.jpg"},
                {"index": 2, "url": "/files/images/movies/SSNI-888/missav-seek/frames/2.jpg"},
            ],
        },
    }


def test_stream_missav_thumbnails_maps_fetch_error_to_failed_completed(monkeypatch):
    class FakeService:
        def get_movie_thumbnails(self, movie_number: str, *, refresh: bool = False, progress_callback=None):
            raise MissavThumbnailRequestError("https://missav.ws/cn/SSNI-888", "bad gateway")

    monkeypatch.setattr(MovieService, "_build_missav_thumbnail_service", lambda: FakeService())

    events = list(MovieService.stream_missav_thumbnails("SSNI-888"))

    assert events == [
        ("search_started", {"movie_number": "SSNI-888", "refresh": False}),
        (
            "completed",
            {
                "success": False,
                "reason": "missav_thumbnail_fetch_failed",
                "detail": "missav thumbnail request failed: https://missav.ws/cn/SSNI-888 (bad gateway)",
            },
        ),
    ]
