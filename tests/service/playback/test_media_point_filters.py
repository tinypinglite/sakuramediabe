import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
    Movie,
    VideoItem,
)
from src.schema.playback.media import MediaPointKind
from src.service.playback.media_service import MediaService


def _library() -> MediaLibrary:
    return MediaLibrary.get_or_create(
        name="point-filter-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]


def _create_point(media: Media, index: int) -> MediaPoint:
    image = Image.create(
        origin=f"point-filter-{index}.webp",
        small=f"point-filter-{index}.webp",
        medium=f"point-filter-{index}.webp",
        large=f"point-filter-{index}.webp",
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=index * 10)
    return MediaPoint.create(
        media=media,
        thumbnail=thumbnail,
        image=image,
        movie_number=media.movie_number,
        video_item_id=media.video_item_id,
        offset_seconds=index * 10,
    )


def _create_jav_point(index: int, movie_number: str | None = None) -> MediaPoint:
    number = movie_number or f"POINTFILTER-{index:03d}"
    movie = Movie.create(
        movie_number=number,
        javdb_id=f"point-filter-{index}",
        title=f"Point filter {index}",
    )
    media = Media.create(movie=movie, library=_library(), file_name=f"point-{index}.mp4")
    return _create_point(media, index)


def _create_video_point(index: int, title: str) -> MediaPoint:
    video = VideoItem.create(title=title)
    media = Media.create(
        video_item=video, library=_library(), file_name=f"video-{index}.mp4"
    )
    return _create_point(media, index)


def test_keyword_matches_normalized_movie_number(test_db):
    first = _create_jav_point(1, "POINTFILTER-001")
    _create_jav_point(2, "POINTFILTER-002")

    page = MediaService.list_media_points(keyword="pointfilter001")

    assert [item.point_id for item in page.items] == [first.id]
    assert page.total == 1


def test_keyword_terms_are_anded(test_db):
    first = _create_jav_point(1, "POINTFILTER-001")
    _create_jav_point(2, "OTHER-001")

    page = MediaService.list_media_points(keyword="POINTFILTER 001")

    assert [item.point_id for item in page.items] == [first.id]
    assert page.total == 1


def test_keyword_matches_video_title_only_for_video_kind(test_db):
    _create_jav_point(1, "POINTFILTER-001")
    video_point = _create_video_point(2, "海边日落片段")

    video_page = MediaService.list_media_points(
        keyword="日落", kind=MediaPointKind.VIDEO
    )
    jav_page = MediaService.list_media_points(keyword="日落", kind=MediaPointKind.JAV)
    all_page = MediaService.list_media_points(keyword="日落", kind=MediaPointKind.ALL)

    assert [item.point_id for item in video_page.items] == [video_point.id]
    assert jav_page.total == 0
    assert [item.point_id for item in all_page.items] == [video_point.id]


def test_keyword_matches_fc2_number_folding(test_db):
    point = _create_jav_point(1, "FC2PPV-1234567")

    page = MediaService.list_media_points(keyword="fc2-1234567")

    assert [item.point_id for item in page.items] == [point.id]


def test_numeric_number_separator_is_significant(test_db):
    point = _create_jav_point(1, "0100123-456")
    _create_jav_point(2, "0100123_456")

    page = MediaService.list_media_points(keyword="0100123-456")

    assert [item.point_id for item in page.items] == [point.id]


def test_keyword_over_limit_raises(test_db):
    with pytest.raises(ApiError) as error:
        MediaService.list_media_points(keyword="x" * 65)

    assert error.value.status_code == 422
    assert error.value.code == "invalid_media_point_filter"


def test_symbol_only_keyword_matches_nothing(test_db):
    _create_jav_point(1, "POINTFILTER-001")

    page = MediaService.list_media_points(keyword="!!!")

    assert page.total == 0


def test_exclude_collection_filters_members(test_db):
    first = _create_jav_point(1, "POINTFILTER-001")
    second = _create_jav_point(2, "POINTFILTER-002")
    third = _create_jav_point(3, "POINTFILTER-003")
    collection = MomentCollection.create(name="过滤合集", description="")
    MomentCollectionItem.create(collection=collection, point=first, position=0)

    page = MediaService.list_media_points(exclude_collection_id=collection.id)

    assert {item.point_id for item in page.items} == {second.id, third.id}
    assert page.total == 2


def test_kind_filter_counts(test_db):
    _create_jav_point(1, "POINTFILTER-001")
    _create_video_point(2, "视频时刻")

    assert MediaService.list_media_points(kind=MediaPointKind.JAV).total == 1
    assert MediaService.list_media_points(kind=MediaPointKind.VIDEO).total == 1
    assert MediaService.list_media_points(kind=MediaPointKind.ALL).total == 2


def test_invalid_page_size_raises(test_db):
    with pytest.raises(ApiError) as error:
        MediaService.list_media_points(page_size=101)

    assert error.value.status_code == 422
    assert error.value.code == "invalid_media_point_filter"
