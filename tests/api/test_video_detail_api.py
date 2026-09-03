from types import SimpleNamespace

from src.model import Media, MediaLibrary, VideoItem
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_video_detail_exposes_media_playback_deliveries(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(name="video-detail-library", provider_key="pornbox", provider_config={})
    video = VideoItem.create(title="video detail")
    media = Media.create(video_item=video, library=library, file_name="detail.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )

    response = client.get(
        f"/videos/{video.id}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.json()["media_items"][0]["media_id"] == media.id
    assert response.json()["media_items"][0]["playback_deliveries"] == ["proxy", "redirect"]


def test_video_list_exposes_first_media_dimensions_for_masonry(client, account_user):
    library = MediaLibrary.create(
        name="video-list-library", provider_key="pornbox", provider_config={}
    )
    video = VideoItem.create(title="portrait video")
    Media.create(
        video_item=video,
        library=library,
        file_name="portrait.mp4",
        resolution=" 720X1280 ",
    )

    response = client.get("/videos", headers=_auth_headers(client, account_user))

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == video.id
    assert item["cover_width"] == 720
    assert item["cover_height"] == 1280
