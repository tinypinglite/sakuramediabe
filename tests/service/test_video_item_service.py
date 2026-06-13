import pytest

from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieSeries,
    ResourceTaskState,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.model.base import database_proxy
from src.schema.videos.items import VideoItemCreateRequest
from src.service.videos import VideoItemService

_MODELS = [
    Image,
    MovieSeries,
    Movie,
    VideoItem,
    VideoCollection,
    VideoCollectionItem,
    MediaLibrary,
    Media,
    MediaThumbnail,
    MediaProgress,
    MediaPoint,
    ResourceTaskState,
]


@pytest.fixture()
def video_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
    test_db.drop_tables(list(reversed(_MODELS)))
    # 还原到全局 proxy，避免把模型永久绑定在已关闭的测试库上污染后续用例。
    for model in _MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)


def test_detail_reports_media_count_and_can_play(video_tables):
    video = VideoItem.create(title="片子")
    Media.create(video_item=video, path="/lib/a.mp4", valid=True, content_fingerprint="fp-a")
    Media.create(video_item=video, path="/lib/b.mp4", valid=False, content_fingerprint="fp-b")

    detail = VideoItemService.get_video_detail(video.id)

    assert detail.media_count == 2
    assert detail.can_play is True
    assert {media.media_id for media in detail.media_items} == {
        media.id for media in Media.select()
    }
    # 媒体资源带签名播放地址。
    assert all(media.play_url for media in detail.media_items)


def test_list_filters_by_query_and_reports_media_count(video_tables):
    video_with = VideoItemService.create_video(VideoItemCreateRequest(title="海边录像"))
    VideoItemService.create_video(VideoItemCreateRequest(title="森林散步"))
    Media.create(
        video_item=VideoItem.get_by_id(video_with.id),
        path="/lib/x.mp4",
        valid=True,
        content_fingerprint="fp-x",
    )

    page = VideoItemService.list_videos(query="海边")

    assert page.total == 1
    assert page.items[0].id == video_with.id
    assert page.items[0].media_count == 1
    assert page.items[0].can_play is True


def test_delete_video_cascades_media(video_tables):
    video = VideoItem.create(title="待删除")
    Media.create(video_item=video, path="/lib/d.mp4", valid=True, content_fingerprint="fp-d")

    VideoItemService.delete_video(video.id)

    assert VideoItem.select().count() == 0
    assert Media.select().count() == 0
