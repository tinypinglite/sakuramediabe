from types import SimpleNamespace

from src.model import (
    Media,
    MediaLibrary,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_delete_video_owned_media_removes_entry_and_collection_membership(
    client,
    account_user,
    monkeypatch,
):
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _: SimpleNamespace(delete_media=lambda **kwargs: None),
    )
    headers = _auth_headers(client, account_user.username)
    library = MediaLibrary.create(
        name="media-delete-video-sync",
        provider_key="demo",
        provider_config={},
    )
    target_video = VideoItem.create(title="delete target")
    other_video = VideoItem.create(title="duplicate copy")
    target_media = Media.create(
        video_item=target_video, library=library, file_name="target.mp4"
    )
    other_media = Media.create(
        video_item=other_video, library=library, file_name="other.mp4"
    )
    collection = VideoCollection.create(name="sync collection")
    VideoCollectionItem.create(collection=collection, video_item=target_video, position=0)
    VideoCollectionItem.create(collection=collection, video_item=other_video, position=1)

    response = client.delete(f"/media/{target_media.id}", headers=headers)

    assert response.status_code == 204, response.text
    assert Media.get_or_none(Media.id == target_media.id) is None
    assert VideoItem.get_or_none(VideoItem.id == target_video.id) is None
    assert not (
        VideoCollectionItem.select()
        .where(VideoCollectionItem.video_item == target_video)
        .exists()
    )
    assert client.get(f"/videos/{target_video.id}", headers=headers).status_code == 404
    # 相同文件的另一条重复视频保持独立，不被连带删除。
    assert Media.get_or_none(Media.id == other_media.id) is not None
    assert VideoItem.get_or_none(VideoItem.id == other_video.id) is not None
    members = client.get(f"/video-collections/{collection.id}/items", headers=headers)
    assert members.status_code == 200, members.text
    assert [item["video"]["id"] for item in members.json()["items"]] == [other_video.id]
