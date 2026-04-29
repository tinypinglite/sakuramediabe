from datetime import datetime

import pytest
from pydantic import ValidationError

from src.model import Actor, Image, Movie, MovieActor, MoviePlotImage, MovieTag, Tag
from src.schema.catalog.actors import (
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    ImageResource,
    YearResource,
)
from src.schema.catalog.movies import (
    MovieDetailResource,
    MovieListItemResource,
    TagResource,
)
from src.schema.common.pagination import PageResponse
from src.schema.system.account import AccountResource
from src.service.catalog.actor_service import ActorService
from src.service.catalog.movie_service import MovieService
from src.service.system.account_service import AccountService


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


def test_account_resource_can_be_built_from_user_attributes(app, account_user):
    resource = AccountService.get_account(account_user)

    assert isinstance(resource, AccountResource)
    assert resource.username == account_user.username
    assert resource.model_dump()["created_at"] == account_user.created_at


def test_tag_resource_can_be_built_from_peewee_model(app):
    tag = Tag.create(name="剧情")

    resource = TagResource(tag_id=tag.id, name=tag.name)

    assert isinstance(resource, TagResource)
    assert resource.model_dump() == {"tag_id": tag.id, "name": "剧情"}


def test_movie_list_item_resource_uses_canonical_field_names_only(app, build_signed_image_url):
    image = Image.create(
        origin="origin.jpg",
        small="small.jpg",
        medium="medium.jpg",
        large="large.jpg",
    )
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        series_name="Series 1",
        cover_image=image,
        release_date=datetime(2024, 1, 2, 3, 4, 5),
        duration_minutes=120,
        score=4.5,
        is_collection=True,
        is_subscribed=False,
    )

    resource = MovieListItemResource.model_validate(
        {
            "javdb_id": movie.javdb_id,
            "movie_number": movie.movie_number,
            "title": movie.title,
            "title_zh": "",
            "series_id": movie.series_id,
            "series_name": movie.series_name,
            "cover_image": image,
            "release_date": movie.release_date,
            "duration_minutes": movie.duration_minutes,
            "score": movie.score,
            "is_collection": movie.is_collection,
            "is_subscribed": movie.is_subscribed,
        }
    )

    assert resource.model_dump() == {
        "javdb_id": "MovieA1",
        "movie_number": "ABC-001",
        "title": "Movie 1",
        "title_zh": "",
        "series_id": movie.series_id,
        "series_name": "Series 1",
        "cover_image": {
            "id": image.id,
            "origin": build_signed_image_url("origin.jpg"),
            "small": build_signed_image_url("small.jpg"),
            "medium": build_signed_image_url("medium.jpg"),
            "large": build_signed_image_url("large.jpg"),
        },
        "thin_cover_image": None,
        "release_date": "2024-01-02",
        "duration_minutes": 120,
        "score": 4.5,
        "watched_count": 0,
        "want_watch_count": 0,
        "comment_count": 0,
        "score_number": 0,
        "heat": 0,
        "is_collection": True,
        "is_subscribed": False,
        "can_play": False,
        "is_4k": False,
    }

    with pytest.raises(ValidationError):
        MovieListItemResource.model_validate(
            {
                "number": movie.movie_number,
                "title": movie.title,
                "post_time": movie.release_date,
                "duration": movie.duration_minutes,
                "score": movie.score,
                "is_collect_number": movie.is_collection,
                "viewer_state": {"is_subscribed": movie.is_subscribed},
            }
        )


def test_actor_service_list_actors_returns_page_schema(app, build_signed_image_url):
    image = Image.create(
        origin="origin.jpg",
        small="small.jpg",
        medium="medium.jpg",
        large="large.jpg",
    )
    _create_actor(
        "三上悠亚",
        "ActorA1",
        alias_name="三上悠亚 / 鬼头桃菜",
        profile_image=image,
        is_subscribed=True,
    )
    _create_actor("鬼头桃菜", "ActorB2", alias_name="三上悠亚 / 鬼头桃菜", is_subscribed=True)

    response = ActorService.list_actors()

    assert isinstance(response, PageResponse)
    assert isinstance(response.items[0], ActorResource)
    assert response.model_dump()["items"][0] == {
        "id": 1,
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


def test_actor_service_list_actors_returns_page_schema_with_filters(app):
    _create_actor("三上悠亚", "ActorA1", gender=1, is_subscribed=True)
    _create_actor("鬼头桃菜", "ActorB2", gender=1, is_subscribed=False)
    _create_actor("森林原人", "ActorC3", gender=2, is_subscribed=True)

    response = ActorService.list_actors(
        gender=ActorListGender.FEMALE,
        subscription_status=ActorListSubscriptionStatus.SUBSCRIBED,
    )

    assert isinstance(response, PageResponse)
    assert isinstance(response.items[0], ActorResource)
    assert response.model_dump() == {
        "items": [
            {
                "id": 1,
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


def test_actor_service_get_actor_years_returns_year_schema(app):
    actor = _create_actor("三上悠亚", "ActorA1")
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        release_date=datetime(2024, 1, 2, 3, 4, 5),
    )
    MovieActor.create(movie=movie, actor=actor)

    years = ActorService.get_actor_years(actor.id)

    assert years == [YearResource(year=2024)]


def test_movie_service_detail_returns_schema_without_manual_field_mapping(
    app,
    build_signed_image_url,
):
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
    tag = Tag.create(name="剧情")
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
    movie = _create_movie(
        "ABC-001",
        "MovieA1",
        title="Movie 1",
        summary="summary",
        series_name="Series 1",
        maker_name="S1 NO.1 STYLE",
        director_name="嵐山みちる",
        cover_image=cover_image,
        thin_cover_image=thin_cover_image,
        release_date=datetime(2024, 1, 2, 3, 4, 5),
        duration_minutes=120,
        score=4.5,
        watched_count=12,
        want_watch_count=23,
        comment_count=34,
        score_number=45,
        heat=9,
        is_collection=True,
        is_subscribed=False,
    )
    image = Image.create(
        origin="plot-origin.jpg",
        small="plot-small.jpg",
        medium="plot-medium.jpg",
        large="plot-large.jpg",
    )
    MovieActor.create(movie=movie, actor=actor_a)
    MovieActor.create(movie=movie, actor=actor_b)
    MovieTag.create(movie=movie, tag=tag)
    MoviePlotImage.create(movie=movie, image=image)

    resource = MovieService.get_movie_detail("ABC-001")

    assert isinstance(resource, MovieDetailResource)
    assert resource.javdb_id == "MovieA1"
    assert resource.series_id == movie.series_id
    assert resource.series_name == "Series 1"
    assert resource.maker_name == "S1 NO.1 STYLE"
    assert resource.director_name == "嵐山みちる"
    assert resource.is_subscribed is False
    assert resource.can_play is False
    assert resource.tags == [TagResource(tag_id=tag.id, name="剧情")]
    assert resource.model_dump()["release_date"] == "2024-01-02"
    assert resource.watched_count == 12
    assert resource.want_watch_count == 23
    assert resource.comment_count == 34
    assert resource.score_number == 45
    assert resource.heat == 9
    assert resource.cover_image == ImageResource(
        id=cover_image.id,
        origin=build_signed_image_url("cover-origin.jpg"),
        small=build_signed_image_url("cover-small.jpg"),
        medium=build_signed_image_url("cover-medium.jpg"),
        large=build_signed_image_url("cover-large.jpg"),
    )
    assert resource.thin_cover_image == ImageResource(
        id=thin_cover_image.id,
        origin=build_signed_image_url("thin-origin.jpg"),
        small=build_signed_image_url("thin-small.jpg"),
        medium=build_signed_image_url("thin-medium.jpg"),
        large=build_signed_image_url("thin-large.jpg"),
    )
    assert resource.actors[0].profile_image == ImageResource(
        id=actor_image.id,
        origin=build_signed_image_url("actor-origin.jpg"),
        small=build_signed_image_url("actor-small.jpg"),
        medium=build_signed_image_url("actor-medium.jpg"),
        large=build_signed_image_url("actor-large.jpg"),
    )
    assert resource.actors[0].gender == 1
    assert resource.actors[1].profile_image is None
    assert resource.actors[1].gender == 2
    assert resource.plot_images == [
        ImageResource(
            id=image.id,
            origin=build_signed_image_url("plot-origin.jpg"),
            small=build_signed_image_url("plot-small.jpg"),
            medium=build_signed_image_url("plot-medium.jpg"),
            large=build_signed_image_url("plot-large.jpg"),
        )
    ]
    assert resource.media_items == []
    assert resource.playlists == []
