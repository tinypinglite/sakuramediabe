import pytest

from src.api.exception.errors import ApiError
from src.model import Movie, MovieTag, Tag
from src.service.catalog.tag_service import TagService


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_tag_service_list_tags_counts_movies_and_includes_empty_tags(app):
    first_tag = Tag.create(name="剧情")
    second_tag = Tag.create(name="制服")
    empty_tag = Tag.create(name="VR")
    first_movie = _create_movie("ABP-120", "MovieA1")
    second_movie = _create_movie("ABP-121", "MovieA2")
    MovieTag.create(movie=first_movie, tag=first_tag)
    MovieTag.create(movie=second_movie, tag=first_tag)
    MovieTag.create(movie=first_movie, tag=second_tag)

    response = TagService.list_tags()

    assert [item.model_dump() for item in response] == [
        {"tag_id": first_tag.id, "name": "剧情", "movie_count": 2},
        {"tag_id": second_tag.id, "name": "制服", "movie_count": 1},
        {"tag_id": empty_tag.id, "name": "VR", "movie_count": 0},
    ]


def test_tag_service_list_tag_movies_reuses_movie_pagination_without_duplicates(app):
    first_tag = Tag.create(name="剧情")
    second_tag = Tag.create(name="制服")
    movie = _create_movie("ABP-120", "MovieA1")
    MovieTag.create(movie=movie, tag=first_tag)
    MovieTag.create(movie=movie, tag=second_tag)

    response = TagService.list_tag_movies(first_tag.id, page=1, page_size=20)

    assert response.total == 1
    assert [item.movie_number for item in response.items] == ["ABP-120"]


def test_tag_service_list_tag_movies_filters_by_director_and_maker(app):
    tag = Tag.create(name="剧情")
    target_movie = _create_movie(
        "ABP-120",
        "MovieA1",
        director_name="嵐山みちる",
        maker_name="S1 NO.1 STYLE",
    )
    same_director_movie = _create_movie(
        "ABP-121",
        "MovieA2",
        director_name="嵐山みちる",
        maker_name="MOODYZ",
    )
    same_maker_movie = _create_movie(
        "ABP-122",
        "MovieA3",
        director_name="別导演",
        maker_name="S1 NO.1 STYLE",
    )
    MovieTag.create(movie=target_movie, tag=tag)
    MovieTag.create(movie=same_director_movie, tag=tag)
    MovieTag.create(movie=same_maker_movie, tag=tag)

    response = TagService.list_tag_movies(
        tag.id,
        director_name="嵐山みちる",
        maker_name="S1 NO.1 STYLE",
    )

    assert response.total == 1
    assert [item.movie_number for item in response.items] == ["ABP-120"]


def test_tag_service_get_tag_raises_not_found(app):
    with pytest.raises(ApiError) as exc_info:
        TagService.get_tag(404)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "tag_not_found"
