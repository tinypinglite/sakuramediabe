from datetime import date, datetime
from functools import partial
from types import SimpleNamespace

import pytest
from playhouse.test_utils import count_queries

from src.model import (
    Actor,
    DailyRecommendationItem,
    Image,
    Media,
    MediaLibrary,
    MediaThumbnail,
    MomentRecommendation,
    Movie,
    MovieActor,
    MovieSeries,
    MovieTag,
    Playlist,
    PlaylistMovie,
    RankingItem,
    Tag,
)
from src.schema.catalog.movies import MovieListStatus
from src.service.catalog.movie_service import MovieService
from src.service.catalog.tag_service import TagService
from src.service.collections.playlist_service import PlaylistService
from src.service.discovery.daily_recommendation_service import (
    DailyRecommendationService,
)
from src.service.discovery.ranking_service import (
    RANKING_SOURCES,
    RankingBoardDefinition,
    RankingCatalogService,
    RankingSourceDefinition,
)


@pytest.fixture()
def media_movies(test_db):
    actor = Actor.create(javdb_id="actor-media", name="Actor", is_subscribed=True)
    series = MovieSeries.create(name="Media series")
    tag = Tag.create(name="media tag")
    playlist = Playlist.create(name="Media playlist")
    libraries = [
        MediaLibrary.create(
            name=name, provider_key="not-installed", provider_config={"secret": "private-library"},
        )
        for name in ["Local", "Cloud"]
    ]
    movies = []
    for i in range(3):
        movie = Movie.create(
            movie_number=f"MEDIA-{i:03d}", javdb_id=f"media-{i}", title=f"Movie {i}",
            series=series, is_collection=False,
        )
        movies.append(movie)
        MovieActor.create(movie=movie, actor=actor)
        MovieTag.create(movie=movie, tag=tag)
        PlaylistMovie.create(playlist=playlist, movie=movie)
        DailyRecommendationItem.create(
            movie=movie, snapshot_date=date(2026, 9, 13), rank=i + 1,
            generated_at=datetime(2026, 9, 13),
        )
    first = Media.create(
        movie=movies[0], library=libraries[0], file_name="first.mp4",
        resolution="3840x2160", duration_seconds=7200, file_size_bytes=9000000000,
        video_info={"container": {"bit_rate": 10000000}, "video": {"bit_rate": 9000000}},
        storage_ref={"private": "private-storage"},
    )
    second = Media.create(movie=movies[0], library=libraries[1], file_name="second.mp4", valid=False)
    invalid = Media.create(movie=movies[1], library=libraries[1], file_name="invalid.mp4", valid=False)
    return SimpleNamespace(
        actor=actor, series=series, tag=tag, playlist=playlist, movies=movies,
        libraries=libraries, media=[first, second, invalid],
    )


def _assert_media_payload(items, data):
    by_number = {item["movie_number"]: item for item in items}
    first = by_number[data.movies[0].movie_number]
    assert first["media_count"] == 2
    assert first["can_play"] is True
    assert [item["media_id"] for item in first["media_items"]] == [m.id for m in data.media[:2]]
    media = first["media_items"][0]
    assert media == {
        "media_id": data.media[0].id,
        "library_id": data.libraries[0].id, "library_name": "Local",
        "provider_key": "not-installed", "file_name": "first.mp4",
        "resolution": "3840x2160", "duration_seconds": 7200,
        "file_size_bytes": 9000000000, "valid": True,
        "video_info": {"container": {"bit_rate": 10000000}, "video": {"bit_rate": 9000000}},
    }
    second = first["media_items"][1]
    assert second["library_name"] == "Cloud"
    assert second["valid"] is False
    assert second["video_info"] is None
    assert second["resolution"] is None
    if data.movies[1].movie_number in by_number:
        invalid = by_number[data.movies[1].movie_number]
        assert invalid["media_count"] == 1
        assert invalid["can_play"] is False
    if data.movies[2].movie_number in by_number:
        empty = by_number[data.movies[2].movie_number]
        assert empty["media_count"] == 0
        assert empty["media_items"] == []
        assert empty["can_play"] is False


def test_all_catalog_lists_return_consistent_media(media_movies):
    data = media_movies
    loaders = [
        MovieService.list_movies,
        partial(MovieService.list_movies, actor_id=data.actor.id),
        MovieService.list_latest_movies,
        MovieService.list_subscribed_actor_latest_movies,
        partial(MovieService.list_movies_by_series, series_id=data.series.id),
        partial(TagService.list_tag_movies, tag_id=data.tag.id),
        partial(PlaylistService.list_playlist_movies, playlist_id=data.playlist.id),
        DailyRecommendationService.list_items,
    ]
    for load in loaders:
        response = load().model_dump(mode="json")
        _assert_media_payload(response["items"], data)
        assert response["total"] == (2 if load == MovieService.list_latest_movies else 3)
    _assert_media_payload(
        [item.model_dump(mode="json") for item in MovieService.search_local_movies("MEDIA-000")],
        data,
    )
    playable = MovieService.list_movies(status=MovieListStatus.PLAYABLE)
    assert playable.total == 1
    assert playable.items[0].media_count == 2


def test_rankings_reuse_media_query(media_movies, monkeypatch):
    data = media_movies
    monkeypatch.setitem(
        RANKING_SOURCES, "test", RankingSourceDefinition(
            key="test", name="Test", boards=(RankingBoardDefinition(key="all", name="All"),),
        ),
    )
    for i, movie in enumerate(data.movies):
        RankingItem.create(source_key="test", board_key="all", rank=i + 1, movie=movie, movie_number=movie.movie_number)
    with count_queries(only_select=True) as queries:
        response = RankingCatalogService.list_board_items("test", "all", None).model_dump(mode="json")
    assert queries.count == 5
    _assert_media_payload(response["items"], data)


def test_movie_media_queries_stay_constant_and_preserve_pagination(test_db):
    library = MediaLibrary.create(name="Bulk", provider_key="not-installed")
    actor = Actor.create(javdb_id="bulk-actor", name="Bulk actor", is_subscribed=True)
    series = MovieSeries.create(name="Bulk series")
    with test_db.atomic():
        for i in range(100):
            movie = Movie.create(movie_number=f"BULK-{i:03d}", javdb_id=f"bulk-{i}", title="Bulk", series=series, is_collection=False)
            MovieActor.create(movie=movie, actor=actor)
            for copy in range(2):
                Media.create(movie=movie, library=library, file_name=f"{i}-{copy}.mp4")
    loaders = [
        MovieService.list_movies,
        partial(MovieService.list_movies, actor_id=actor.id),
        MovieService.list_latest_movies,
        MovieService.list_subscribed_actor_latest_movies,
        partial(MovieService.list_movies_by_series, series_id=series.id),
    ]
    for load in loaders:
        for page_size in (1, 20, 100):
            with count_queries(only_select=True) as queries:
                payload = load(page_size=page_size).model_dump(mode="json")
            assert queries.count == 3
            assert payload["total"] == 100
            assert len(payload["items"]) == page_size
            assert all(item["media_count"] == 2 for item in payload["items"])
        page1 = load(page=1, page_size=20)
        page2 = load(page=2, page_size=20)
        assert not {item.id for item in page1.items} & {item.id for item in page2.items}
        with count_queries(only_select=True) as queries:
            empty = load(page=6, page_size=20).model_dump(mode="json")
        assert queries.count == 2
        assert empty["items"] == []
        assert empty["total"] == 100


def test_similar_movies_return_media_without_additional_flag_query(media_movies):
    from src.service.discovery.recommendation_service import MovieRecommendationService

    data = media_movies
    store = SimpleNamespace(search_many=lambda ids, limit: {
        ids[0]: [SimpleNamespace(movie_id=movie.id, score=0.9) for movie in data.movies],
    })
    service = MovieRecommendationService(store=store)
    with count_queries(only_select=True) as queries:
        items = [item.model_dump(mode="json") for item in service.list_similar_resources("MEDIA-000")]
    assert queries.count == 3
    _assert_media_payload(items, data)
    assert all(item["similarity_score"] == 0.9 for item in items)


def test_moment_recommendations_include_all_media_for_the_movie(media_movies):
    from src.service.discovery.moment_recommendation_service import (
        MomentRecommendationService,
    )

    data = media_movies
    image = Image.create(origin="thumb.jpg", small="thumb.jpg", medium="thumb.jpg", large="thumb.jpg")
    thumbnail = MediaThumbnail.create(media=data.media[0], image=image, offset=30)
    MomentRecommendation.create(
        movie=data.movies[0], media=data.media[0], thumbnail=thumbnail,
        offset_seconds=30, rank=1, strategy="test", reason="test",
        generated_at=datetime(2026, 9, 13),
    )
    response = MomentRecommendationService.list_items().model_dump(mode="json")
    assert response["total"] == 1
    _assert_media_payload([item["movie"] for item in response["items"]], data)


def test_movie_list_api_serializes_media(client, account_user, media_movies):
    token = client.post("/auth/tokens", json={"username": "account", "password": "password123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for path in ["/movies", f"/movies?actor_id={media_movies.actor.id}", "/movies/latest"]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        _assert_media_payload(response.json()["items"], media_movies)
