import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Image,
    Media,
    MediaLibrary,
    Movie,
    MovieSeries,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.model.base import database_proxy
from src.schema.videos.collections import (
    VideoCollectionCreateRequest,
    VideoCollectionUpdateRequest,
)
from src.service.videos import VideoCollectionService

_MODELS = [
    Image,
    MovieSeries,
    Movie,
    VideoItem,
    VideoCollection,
    VideoCollectionItem,
    MediaLibrary,
    Media,
]


@pytest.fixture()
def collection_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
    test_db.drop_tables(list(reversed(_MODELS)))
    # 还原到全局 proxy，避免把模型永久绑定在已关闭的测试库上污染后续用例。
    for model in _MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)


def test_add_item_is_idempotent(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    video = VideoItem.create(title="片")

    VideoCollectionService.add_item(collection.id, video.id)
    VideoCollectionService.add_item(collection.id, video.id)

    assert VideoCollectionItem.select().count() == 1
    assert VideoCollectionService.get_collection(collection.id).item_count == 1


def test_create_collection_rejects_duplicate_name(collection_tables):
    VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="独一"))
    with pytest.raises(ApiError) as exc:
        VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="独一"))
    assert exc.value.code == "video_collection_name_conflict"


def test_delete_collection_keeps_video_items(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    video = VideoItem.create(title="保留")
    VideoCollectionService.add_item(collection.id, video.id)

    VideoCollectionService.delete_collection(collection.id)

    assert VideoCollection.select().count() == 0
    assert VideoCollectionItem.select().count() == 0
    # 删除合集不应删除视频条目本身。
    assert VideoItem.select().where(VideoItem.id == video.id).count() == 1


def test_update_collection_renames(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="旧名"))
    updated = VideoCollectionService.update_collection(
        collection.id, VideoCollectionUpdateRequest(name="新名", description="描述")
    )
    assert updated.name == "新名"
    assert updated.description == "描述"
