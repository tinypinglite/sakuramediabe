from types import SimpleNamespace

from src.model import Image, Media, MediaLibrary, MediaThumbnail, VideoItem
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


def test_video_update_replaces_cover_with_its_thumbnail(
    client, account_user, monkeypatch
):
    library = MediaLibrary.create(
        name="video-cover-library", provider_key="pornbox", provider_config={}
    )
    old_image = Image.create(
        origin="videos/auto-cover.webp",
        small="videos/auto-cover.webp",
        medium="videos/auto-cover.webp",
        large="videos/auto-cover.webp",
    )
    video = VideoItem.create(title="cover video", cover_image=old_image)
    media = Media.create(video_item=video, library=library, file_name="cover.mp4")
    image = Image.create(
        origin="videos/cover-thumbnail.webp",
        small="videos/cover-thumbnail.webp",
        medium="videos/cover-thumbnail.webp",
        large="videos/cover-thumbnail.webp",
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=20)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )

    response = client.patch(
        f"/videos/{video.id}",
        json={"cover_thumbnail_id": thumbnail.id},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert VideoItem.get_by_id(video.id).cover_image_id == image.id
    assert response.json()["cover_image"]["origin"].startswith(
        f"/files/images/{image.origin}?"
    )
    assert Image.get_or_none(id=old_image.id) is None


def test_video_update_rejects_thumbnail_from_another_video(client, account_user):
    library = MediaLibrary.create(
        name="video-cover-owner-library", provider_key="pornbox", provider_config={}
    )
    video = VideoItem.create(title="cover owner")
    other_video = VideoItem.create(title="other video")
    media = Media.create(video_item=other_video, library=library, file_name="other.mp4")
    image = Image.create(
        origin="videos/other-thumbnail.webp",
        small="videos/other-thumbnail.webp",
        medium="videos/other-thumbnail.webp",
        large="videos/other-thumbnail.webp",
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=10)

    response = client.patch(
        f"/videos/{video.id}",
        json={"cover_thumbnail_id": thumbnail.id},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "video_cover_thumbnail_not_found"
    assert VideoItem.get_by_id(video.id).cover_image_id is None
