from src.model import Image, Media, MediaLibrary, MediaPoint, MediaThumbnail, Movie
from src.schema.collections.moments import MomentCollectionCreateRequest
from src.service.collections.moment_collection_service import MomentCollectionService


def _create_point(index: int) -> MediaPoint:
    library = MediaLibrary.get_or_create(
        name="moment-collection-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]
    movie = Movie.create(
        movie_number=f"MOMENT-COLLECTION-{index:03d}",
        javdb_id=f"moment-collection-{index}",
        title=f"Moment collection {index}",
    )
    media = Media.create(movie=movie, library=library, file_name=f"moment-{index}.mp4")
    image = Image.create(
        origin=f"origin-{index}",
        small=f"small-{index}",
        medium=f"medium-{index}",
        large=f"large-{index}",
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=index * 10)
    return MediaPoint.create(
        media=media, thumbnail=thumbnail, offset_seconds=index * 10
    )


def test_moment_collection_preserves_order_and_reports_memberships(test_db):
    first = _create_point(1)
    second = _create_point(2)
    collection = MomentCollectionService.create_collection(
        MomentCollectionCreateRequest(name="旅行片段", description="按时间整理")
    )

    MomentCollectionService.add_point(collection.id, first.id)
    MomentCollectionService.add_point(collection.id, second.id)
    MomentCollectionService.set_points(collection.id, [second.id, first.id, second.id])

    first_page = MomentCollectionService.list_collection_points(
        collection.id, page=1, page_size=1
    )
    second_page = MomentCollectionService.list_collection_points(
        collection.id, page=2, page_size=1
    )
    memberships = MomentCollectionService.list_point_collections(first.id)
    summary = MomentCollectionService.get_collection(collection.id)

    assert [item.point_id for item in first_page.items] == [second.id]
    assert [item.point_id for item in second_page.items] == [first.id]
    assert first_page.total == second_page.total == 2
    assert [item.position for item in first_page.items + second_page.items] == [0, 1]
    assert [(item.id, item.name) for item in memberships] == [
        (collection.id, "旅行片段")
    ]
    assert summary.point_count == 2
    assert summary.cover_image is not None
