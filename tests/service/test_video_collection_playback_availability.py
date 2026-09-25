from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from src.model import VideoCollection, VideoCollectionItem, VideoItem
from src.plugins.provider_protocol import ProviderUnavailableError
from src.service.videos import video_collection_service
from src.service.videos.video_collection_service import VideoCollectionService
from src.service.videos.video_item_service import VideoItemService


def test_collection_keeps_members_when_one_provider_is_missing(monkeypatch):
    # 仅替换数据库查询；保留分页入口、资源组装、签名地址与插件错误语义。
    links = []
    for item_id, provider_key, media_id in [(1, "missing", 101), (2, "local", 102), (3, "", 0)]:
        links.append(SimpleNamespace(
            id=item_id,
            position=item_id - 1,
            video_item_id=item_id,
            video_item=VideoItem(
                id=item_id,
                title=f"video-{item_id}",
                created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1),
            ),
            play_media_id=media_id,
            play_provider_key=provider_key,
            first_duration_seconds=10,
            first_file_size_bytes=100,
            first_resolution="1920x1080",
        ))
    query = MagicMock()
    for method in ("join", "switch", "where", "order_by", "limit"):
        getattr(query, method).return_value = query
    query.count.return_value = len(links)
    query.__iter__.return_value = iter(links)
    monkeypatch.setattr(VideoCollectionItem, "select", lambda *_args: query)
    monkeypatch.setattr(
        VideoCollectionService, "_require_collection", lambda _id: VideoCollection(id=1)
    )
    monkeypatch.setattr(
        VideoItemService, "_media_stats", lambda _ids: {1: (1, True), 2: (1, True)}
    )

    def require(provider_key):
        if provider_key == "missing":
            raise ProviderUnavailableError(provider_key)
        assert provider_key == "local"
        return SimpleNamespace(playback_deliveries=("proxy",))

    monkeypatch.setattr(
        video_collection_service, "MEDIA_PROVIDER_REGISTRY", SimpleNamespace(require=require)
    )

    result = VideoCollectionService.list_collection_items(1, include_play_url=True)

    assert result.total == 3
    assert [item.video.title for item in result.items] == ["video-1", "video-2", "video-3"]
    unavailable, playable, empty = result.items
    assert unavailable.play_url is None
    assert unavailable.video.can_play is False
    assert unavailable.first_media_id == 101
    assert playable.video.can_play is True
    url = urlsplit(playable.play_url)
    assert url.path == "/media/102/play/"
    assert parse_qs(url.query)["delivery"] == ["proxy"]
    assert empty.play_url is None
    assert empty.video.can_play is False


def test_collection_items_prefer_first_valid_media_for_play_url(test_db, monkeypatch):
    from src.model import (
        Media,
        MediaLibrary,
        VideoCollection,
        VideoCollectionItem,
        VideoItem,
    )

    library = MediaLibrary.create(
        name="collection-validity", provider_key="demo", provider_config={}
    )
    mixed_video = VideoItem.create(title="mixed")
    invalid_first = Media.create(
        video_item=mixed_video, library=library, file_name="bad.mp4", valid=False
    )
    valid_second = Media.create(
        video_item=mixed_video, library=library, file_name="good.mp4"
    )
    empty_video = VideoItem.create(title="empty")
    Media.create(
        video_item=empty_video, library=library, file_name="bad-only.mp4", valid=False
    )
    collection = VideoCollection.create(name="validity")
    VideoCollectionItem.create(collection=collection, video_item=mixed_video, position=0)
    VideoCollectionItem.create(collection=collection, video_item=empty_video, position=1)
    monkeypatch.setattr(
        video_collection_service,
        "MEDIA_PROVIDER_REGISTRY",
        SimpleNamespace(
            require=lambda _provider_key: SimpleNamespace(
                playback_deliveries=("proxy",)
            )
        ),
    )

    result = VideoCollectionService.list_collection_items(
        collection.id, include_play_url=True
    )

    # 失效副本 id 更小，但播放地址必须落到有效副本。
    assert invalid_first.id < valid_second.id
    mixed_item, empty_item = result.items
    assert mixed_item.first_media_id == valid_second.id
    assert mixed_item.play_url is not None
    assert f"/media/{valid_second.id}/play/" in mixed_item.play_url
    assert mixed_item.video.can_play is True
    # 全部失效：无播放地址、无首媒体、不可播，三者语义一致。
    assert empty_item.first_media_id is None
    assert empty_item.play_url is None
    assert empty_item.video.can_play is False
