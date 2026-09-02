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


def test_duplicate_media_groups_are_global_across_libraries_and_kind_specific(
    client,
    account_user,
):
    local_library = MediaLibrary.create(
        name="duplicate-local",
        provider_key="local",
        provider_config={},
    )
    cloud_library = MediaLibrary.create(
        name="duplicate-cloud",
        provider_key="cloud",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="DUP-001",
        javdb_id="duplicate-jav-001",
        title="duplicate jav",
    )
    first_video = VideoItem.create(title="duplicate video one")
    second_video = VideoItem.create(title="duplicate video two")
    alpha_collection = VideoCollection.create(name="duplicate alpha")
    beta_collection = VideoCollection.create(name="duplicate beta")
    VideoCollectionItem.create(collection=beta_collection, video_item=first_video)
    VideoCollectionItem.create(collection=alpha_collection, video_item=first_video)
    shared_hash = "media-file-hash-v1:" + "a" * 40

    first_jav = Media.create(
        movie=movie,
        library=local_library,
        file_name="duplicate-jav-local.mp4",
        file_hash=shared_hash,
    )
    second_jav = Media.create(
        movie=movie,
        library=cloud_library,
        file_name="duplicate-jav-cloud.mp4",
        file_hash=shared_hash,
    )
    first_video_media = Media.create(
        video_item=first_video,
        library=local_library,
        file_name="duplicate-video-local.mp4",
        file_hash=shared_hash,
    )
    second_video_media = Media.create(
        video_item=second_video,
        library=cloud_library,
        file_name="duplicate-video-cloud.mp4",
        file_hash=shared_hash,
    )
    Media.create(
        movie=movie,
        library=local_library,
        file_name="missing-hash.mp4",
    )

    headers = _auth_headers(client, account_user.username)
    jav_response = client.get(
        "/media/duplicates", params={"kind": "jav"}, headers=headers
    )
    video_response = client.get(
        "/media/duplicates", params={"kind": "video"}, headers=headers
    )

    assert jav_response.status_code == 200
    jav_body = jav_response.json()
    assert jav_body["total"] == 1
    assert jav_body["items"][0]["kind"] == "jav"
    assert jav_body["items"][0]["media_count"] == 2
    assert {item["id"] for item in jav_body["items"][0]["media_items"]} == {
        first_jav.id,
        second_jav.id,
    }
    assert {item["library_name"] for item in jav_body["items"][0]["media_items"]} == {
        local_library.name,
        cloud_library.name,
    }

    assert video_response.status_code == 200
    video_body = video_response.json()
    assert video_body["total"] == 1
    assert video_body["items"][0]["kind"] == "video"
    assert video_body["items"][0]["media_count"] == 2
    assert {item["id"] for item in video_body["items"][0]["media_items"]} == {
        first_video_media.id,
        second_video_media.id,
    }
    collections_by_video_id = {
        item["video_item_id"]: item["collections"]
        for item in video_body["items"][0]["media_items"]
    }
    assert collections_by_video_id[first_video.id] == [
        {"id": alpha_collection.id, "name": alpha_collection.name},
        {"id": beta_collection.id, "name": beta_collection.name},
    ]
    assert collections_by_video_id[second_video.id] == []
