from src.model import (
    Media,
    MediaLibrary,
    Movie,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_list_returns_pornbox_collections(client, account_user):
    library = MediaLibrary.create(
        name="media-list-collections",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="COLLECTION-JAV-001",
        title="Collection JAV",
    )
    video = VideoItem.create(title="Collection video")
    alpha = VideoCollection.create(name="alpha collection")
    beta = VideoCollection.create(name="beta collection")
    VideoCollectionItem.create(collection=beta, video_item=video)
    VideoCollectionItem.create(collection=alpha, video_item=video)
    jav_media = Media.create(
        movie=movie,
        library=library,
        file_name="collection-jav.mp4",
    )
    video_media = Media.create(
        video_item=video,
        library=library,
        file_name="collection-video.mp4",
    )

    response = client.get(
        "/media",
        params={"kind": "video"},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == video_media.id
    assert item["collections"] == [
        {"id": alpha.id, "name": alpha.name},
        {"id": beta.id, "name": beta.name},
    ]

    jav_response = client.get(
        "/media",
        params={"kind": "jav"},
        headers=_auth_headers(client, account_user.username),
    )

    assert jav_response.status_code == 200
    jav_item = jav_response.json()["items"][0]
    assert jav_item["id"] == jav_media.id
    assert jav_item["collections"] == []
