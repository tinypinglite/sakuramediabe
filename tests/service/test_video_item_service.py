import pytest

from src.api.exception.errors import ApiError
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


def test_delete_video_cleans_up_cover_image(video_tables, tmp_path, monkeypatch):
    # 把图片根目录指向临时目录，便于断言封面 webp 被物理删除。
    monkeypatch.setattr(
        "src.service.catalog.image_cleanup_service.ImageCleanupService.image_root_path",
        classmethod(lambda cls: tmp_path),
    )
    cover_rel = "videos/1/cover/0.webp"
    cover_file = tmp_path / cover_rel
    cover_file.parent.mkdir(parents=True, exist_ok=True)
    cover_file.write_bytes(b"cover")
    image = Image.create(origin=cover_rel, small=cover_rel, medium=cover_rel, large=cover_rel)
    video = VideoItem.create(title="带封面", cover_image=image)

    VideoItemService.delete_video(video.id)

    # 删视频后封面 Image 行与磁盘文件都不应残留。
    assert VideoItem.select().count() == 0
    assert Image.select().count() == 0
    assert cover_file.exists() is False


def test_list_videos_preloads_cover_without_n_plus_1(video_tables):
    from playhouse.test_utils import count_queries

    for index in range(5):
        image = Image.create(
            origin=f"c{index}.webp",
            small=f"c{index}.webp",
            medium=f"c{index}.webp",
            large=f"c{index}.webp",
        )
        VideoItem.create(title=f"v{index}", cover_image=image)

    with count_queries() as counter:
        page = VideoItemService.list_videos(page=1, page_size=10)

    assert len(page.items) == 5
    assert all(item.cover_image is not None for item in page.items)
    # 查询数固定（count + 预加载 select + media_stats），不随条目数线性增长；N+1 会退化成 3+5。
    assert counter.count <= 4


def _make_video_with_media(title, media_specs):
    """按顺序创建条目及其媒体；media_specs 为 (duration_seconds, file_size_bytes) 列表，先建者 Media.id 更小（即第一条）。"""
    video = VideoItem.create(title=title)
    for index, (duration, size) in enumerate(media_specs):
        Media.create(
            video_item=video,
            path=f"/lib/{title}-{index}.mp4",
            valid=True,
            content_fingerprint=f"fp-{title}-{index}",
            duration_seconds=duration,
            file_size_bytes=size,
        )
    return video


def test_list_videos_sorts_by_first_media_duration_and_size(video_tables):
    # A 第一条 100s/50B，第二条 999s/9999B：用于验证排序取「第一条媒体」而非最大值。
    a = _make_video_with_media("A", [(100, 50), (999, 9999)])
    b = _make_video_with_media("B", [(200, 20)])

    by_duration = VideoItemService.list_videos(sort="duration:desc")
    # 取第一条：B(200) 在 A(100) 前；若误取最大值，A(999) 会排到最前。
    assert [item.id for item in by_duration.items] == [b.id, a.id]
    rows = {item.id: item for item in by_duration.items}
    assert rows[a.id].duration_seconds == 100
    assert rows[a.id].file_size_bytes == 50
    assert rows[b.id].duration_seconds == 200

    by_size = VideoItemService.list_videos(sort="file_size:desc")
    # 第一条文件大小：A(50B) > B(20B)。
    assert [item.id for item in by_size.items] == [a.id, b.id]


def test_list_videos_treats_media_less_item_as_zero(video_tables):
    a = _make_video_with_media("A", [(100, 50)])
    b = VideoItem.create(title="无媒体")

    page = VideoItemService.list_videos(sort="duration:desc")
    # 无媒体条目按 0 参与排序，降序时排在最后。
    assert [item.id for item in page.items] == [a.id, b.id]
    zero = {item.id: item for item in page.items}[b.id]
    assert zero.duration_seconds == 0
    assert zero.file_size_bytes == 0
    assert zero.media_count == 0


def test_list_videos_sort_by_title_asc(video_tables):
    VideoItem.create(title="banana")
    VideoItem.create(title="apple")

    page = VideoItemService.list_videos(sort="title:asc")

    assert [item.title for item in page.items] == ["apple", "banana"]


def test_list_videos_rejects_unsupported_sort(video_tables):
    VideoItem.create(title="x")
    # release_date 排序已移除；position 仅合集可用；方向/格式非法也应拒绝。
    for bad in ("release_date:desc", "position:asc", "title:upward", "garbage"):
        with pytest.raises(ApiError) as exc:
            VideoItemService.list_videos(sort=bad)
        assert exc.value.code == "invalid_video_filter"


def test_video_detail_reports_first_media_duration_and_size(video_tables):
    video = _make_video_with_media("D", [(120, 60), (999, 9999)])

    detail = VideoItemService.get_video_detail(video.id)

    # 详情时长/大小同样取第一条媒体。
    assert detail.duration_seconds == 120
    assert detail.file_size_bytes == 60
