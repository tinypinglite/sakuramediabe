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
