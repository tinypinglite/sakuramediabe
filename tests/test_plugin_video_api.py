"""插件视频门面契约：只读快照与视频合集成员管理。"""

from __future__ import annotations

import pytest

from src.api.exception.errors import ApiError
from src.model import Media, MediaLibrary, VideoCollection, VideoItem
from src.plugins import (
    PluginCollection,
    PluginContext,
    PluginVideoPage,
    PluginVideoSnapshot,
)


def _library(name: str = "视频库") -> MediaLibrary:
    return MediaLibrary.create(name=name, provider_key="local", provider_config={})


def _video(title: str, *, summary: str = "", **overrides) -> VideoItem:
    return VideoItem.create(title=title, summary=summary, **overrides)


def _media(
    video: VideoItem,
    library: MediaLibrary,
    *,
    file_name: str,
    valid: bool = True,
    duration_seconds: int = 0,
    file_size_bytes: int = 0,
    resolution: str | None = None,
) -> Media:
    return Media.create(
        video_item=video,
        library=library,
        file_name=file_name,
        valid=valid,
        duration_seconds=duration_seconds,
        file_size_bytes=file_size_bytes,
        resolution=resolution,
    )


def test_video_snapshot_maps_first_valid_media_and_collections(test_db, tmp_path):
    library = _library()
    video = _video("片段A", summary="描述")
    # 第一条失效、第二条有效：统计包含全部媒体，媒体字段取第一条有效媒体。
    _media(video, library, file_name="broken.mp4", valid=False, duration_seconds=1)
    _media(
        video,
        library,
        file_name="good.mp4",
        duration_seconds=42,
        file_size_bytes=2048,
        resolution="1920x1080",
    )
    invalid_only = _video("只有失效媒体")
    _media(invalid_only, library, file_name="gone.mp4", valid=False)

    context = PluginContext("video_demo", {}, tmp_path)
    collection = context.collections.ensure_video_collection("精选")
    context.collections.add_video_items("精选", [video.id])

    snapshot = context.videos.get(video.id)
    assert isinstance(snapshot, PluginVideoSnapshot)
    assert snapshot.video_id == video.id
    assert snapshot.title == "片段A"
    assert snapshot.summary == "描述"
    assert snapshot.media_count == 2
    assert snapshot.has_playable is True
    assert snapshot.duration_seconds == 42
    assert snapshot.file_size_bytes == 2048
    assert snapshot.resolution == "1920x1080"
    assert snapshot.file_name == "good.mp4"
    assert snapshot.collection_ids == (collection.collection_id,)

    empty = context.videos.get(invalid_only.id)
    assert empty.media_count == 1
    assert empty.has_playable is False
    assert empty.duration_seconds == 0
    assert empty.file_size_bytes == 0
    assert empty.resolution is None
    assert empty.file_name is None
    assert empty.collection_ids == ()
    assert context.videos.get(999999) is None


def test_video_list_page_cursor_and_limit_validation(test_db, tmp_path):
    context = PluginContext("video_demo", {}, tmp_path)
    first = _video("一")
    second = _video("二")
    third = _video("三")

    page = context.videos.list_page(limit=2)
    assert isinstance(page, PluginVideoPage)
    assert [item.video_id for item in page.items] == [first.id, second.id]
    assert page.next_cursor == second.id

    rest = context.videos.list_page(after_id=page.next_cursor, limit=2)
    assert [item.video_id for item in rest.items] == [third.id]
    assert rest.next_cursor is None

    with pytest.raises(ValueError):
        context.videos.list_page(after_id=-1)
    with pytest.raises(ValueError):
        context.videos.list_page(limit=0)
    with pytest.raises(ValueError):
        context.videos.list_page(limit=1001)


def test_video_collection_membership_add_and_remove(test_db, tmp_path):
    _library()
    first = _video("一")
    second = _video("二")
    context = PluginContext("video_demo", {}, tmp_path)
    created = context.collections.ensure_video_collection("精选")
    direct = VideoCollection.create(name="用户合集")

    added = context.collections.add_video_items("精选", [first.id, second.id, first.id])
    assert isinstance(added, PluginCollection)
    assert added.collection_type == "video"
    assert added.member_count == 2
    # 幂等：重复添加不改变成员数；按 id 或名称都可定位合集。
    assert context.collections.add_video_items(
        created.collection_id, [first.id]
    ).member_count == 2
    assert context.collections.add_video_items("用户合集", [first.id]).member_count == 1
    assert context.videos.get(first.id).collection_ids == (
        created.collection_id,
        direct.id,
    )

    removed = context.collections.remove_video_items("精选", [second.id, 999999])
    assert removed.member_count == 1
    # 再次移除同一成员幂等。
    assert context.collections.remove_video_items("精选", [second.id]).member_count == 1

    with pytest.raises(ApiError) as missing_video:
        context.collections.add_video_items("精选", [999999])
    assert missing_video.value.status_code == 404
    assert missing_video.value.code == "video_item_not_found"

    with pytest.raises(ApiError) as missing_collection:
        context.collections.add_video_items("不存在", [first.id])
    assert missing_collection.value.status_code == 404
    assert missing_collection.value.code == "video_collection_not_found"

    with pytest.raises(ValueError):
        context.collections.remove_video_items("精选", [])
