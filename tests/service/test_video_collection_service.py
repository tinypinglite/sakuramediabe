import pytest

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    Image,
    Media,
    MediaLibrary,
    Movie,
    MovieSeries,
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
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
    BackgroundTaskRun,
    VideoImportJob,
]


@pytest.fixture()
def collection_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
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


def test_add_item_idempotent_on_unique_race(collection_tables, monkeypatch):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="赛道"))
    video = VideoItem.create(title="片")
    VideoCollectionService.add_item(collection.id, video.id)

    # 模拟并发：去重 get_or_none 漏看已存在成员，再次加入应命中唯一约束 (collection,video_item) 并幂等返回，不抛 500。
    monkeypatch.setattr(VideoCollectionItem, "get_or_none", lambda *args, **kwargs: None)

    VideoCollectionService.add_item(collection.id, video.id)

    assert VideoCollectionItem.select().count() == 1


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


def test_list_collection_items_default_keeps_position_order(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    a = VideoItem.create(title="A")
    b = VideoItem.create(title="B")
    VideoCollectionService.add_item(collection.id, a.id)
    VideoCollectionService.add_item(collection.id, b.id)

    result = VideoCollectionService.list_collection_items(collection.id)

    # 不传 sort 默认按手动 position 升序。
    assert result.total == 2
    assert [it.video.id for it in result.items] == [a.id, b.id]
    assert [it.position for it in result.items] == [0, 1]
    # 默认不内联播放地址。
    assert all(it.play_url is None for it in result.items)


def test_list_collection_items_sort_by_duration_overrides_position(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    a = VideoItem.create(title="A")
    Media.create(video_item=a, path="/lib/a0.mp4", valid=True, content_fingerprint="fa0", duration_seconds=100, file_size_bytes=50)
    Media.create(video_item=a, path="/lib/a1.mp4", valid=True, content_fingerprint="fa1", duration_seconds=999, file_size_bytes=9999)
    b = VideoItem.create(title="B")
    Media.create(video_item=b, path="/lib/b0.mp4", valid=True, content_fingerprint="fb0", duration_seconds=200, file_size_bytes=20)
    VideoCollectionService.add_item(collection.id, a.id)  # position 0
    VideoCollectionService.add_item(collection.id, b.id)  # position 1

    result = VideoCollectionService.list_collection_items(collection.id, sort="duration:desc")

    # 第一条媒体：B(200) 在 A(100) 前，覆盖 position 顺序（而非取 A 的最大值 999）。
    assert [it.video.id for it in result.items] == [b.id, a.id]
    by_id = {it.video.id: it.video for it in result.items}
    assert by_id[a.id].duration_seconds == 100
    assert by_id[a.id].file_size_bytes == 50
    assert by_id[b.id].duration_seconds == 200


def test_list_collection_items_rejects_invalid_sort(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    with pytest.raises(ApiError) as exc:
        VideoCollectionService.list_collection_items(collection.id, sort="release_date:desc")
    assert exc.value.code == "invalid_video_filter"


def test_list_collection_items_paginates(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    videos = [VideoItem.create(title=f"V{i}") for i in range(3)]
    for video in videos:
        VideoCollectionService.add_item(collection.id, video.id)

    page1 = VideoCollectionService.list_collection_items(collection.id, page=1, page_size=2)
    page2 = VideoCollectionService.list_collection_items(collection.id, page=2, page_size=2)

    assert page1.total == 3 and page2.total == 3
    assert [it.video.id for it in page1.items] == [videos[0].id, videos[1].id]
    assert [it.video.id for it in page2.items] == [videos[2].id]


def test_list_collection_items_rejects_invalid_page(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    with pytest.raises(ApiError) as exc:
        VideoCollectionService.list_collection_items(collection.id, page=0)
    assert exc.value.code == "invalid_video_filter"


def test_list_collection_items_include_play_url(collection_tables):
    collection = VideoCollectionService.create_collection(VideoCollectionCreateRequest(name="C"))
    playable = VideoItem.create(title="可播")
    media = Media.create(
        video_item=playable,
        path="/lib/p0.mp4",
        valid=True,
        content_fingerprint="fp0",
        duration_seconds=100,
        file_size_bytes=50,
    )
    no_media = VideoItem.create(title="无媒体")
    VideoCollectionService.add_item(collection.id, playable.id)
    VideoCollectionService.add_item(collection.id, no_media.id)

    result = VideoCollectionService.list_collection_items(collection.id, include_play_url=True)

    by_id = {it.video.id: it for it in result.items}
    # 有媒体成员内联首个媒体的签名播放地址；无媒体成员为 None。
    assert by_id[playable.id].play_url is not None
    assert f"/media/{media.id}/" in by_id[playable.id].play_url
    assert by_id[no_media.id].play_url is None
    # first_media_id 与 play_url 同源：有媒体成员回首个媒体 id，无媒体成员为 None。
    assert by_id[playable.id].first_media_id == media.id
    assert by_id[no_media.id].first_media_id is None

    # 不传 include_play_url 时不生成地址（默认行为，省去签名开销）；但 first_media_id 恒返回。
    without = VideoCollectionService.list_collection_items(collection.id)
    assert all(it.play_url is None for it in without.items)
    without_by_id = {it.video.id: it for it in without.items}
    assert without_by_id[playable.id].first_media_id == media.id
    assert without_by_id[no_media.id].first_media_id is None
