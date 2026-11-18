from src.model import Image, VideoCollection, VideoCollectionItem, VideoItem
from src.service.videos.video_collection_service import VideoCollectionService


def _name_map():
    return {resource.name: resource for resource in VideoCollectionService.list_collections()}


def test_list_collections_batch_loads_covers_in_position_order(test_db):
    first_cover = Image.create(origin="collection-cover-first.jpg")
    later_cover = Image.create(origin="collection-cover-later.jpg")
    VideoCollection.create(name="空合集", description="")
    collection = VideoCollection.create(name="系列 A", description="")
    first = VideoItem.create(title="第一集", cover_image=first_cover)
    later = VideoItem.create(title="第二集", cover_image=later_cover)
    # position 0 的成员排在前面，封面取它而不是"第一个有封面的成员"。
    VideoCollectionItem.create(collection=collection, video_item=later, position=0)
    VideoCollectionItem.create(collection=collection, video_item=first, position=1)

    no_cover_video = VideoItem.create(title="无封面视频")
    no_cover_collection = VideoCollection.create(name="系列 B", description="")
    VideoCollectionItem.create(
        collection=no_cover_collection, video_item=no_cover_video, position=0
    )

    resources = _name_map()

    assert resources["空合集"].item_count == 0
    assert resources["空合集"].cover_image is None
    assert resources["系列 A"].item_count == 2
    assert resources["系列 A"].cover_image is not None
    # origin 会被签名成 /files/images/... URL，这里断言文件名即可。
    assert "collection-cover-later.jpg" in resources["系列 A"].cover_image.origin
    assert resources["系列 B"].item_count == 1
    assert resources["系列 B"].cover_image is None

    # 单合集路径与批量路径语义一致。
    detail = VideoCollectionService.get_collection(collection.id)
    assert detail.cover_image is not None
    assert "collection-cover-later.jpg" in detail.cover_image.origin


def test_list_collections_cover_ignores_later_members_with_covers(test_db):
    cover = Image.create(origin="collection-cover-only-late.jpg")
    collection = VideoCollection.create(name="封面在第二集", description="")
    first = VideoItem.create(title="第一集无封面")
    later = VideoItem.create(title="第二集有封面", cover_image=cover)
    VideoCollectionItem.create(collection=collection, video_item=first, position=0)
    VideoCollectionItem.create(collection=collection, video_item=later, position=1)

    resources = _name_map()

    assert resources["封面在第二集"].item_count == 2
    # 与单合集路径一致：最前成员没有封面时，整集合集封面为空。
    assert resources["封面在第二集"].cover_image is None
    assert VideoCollectionService.get_collection(collection.id).cover_image is None
