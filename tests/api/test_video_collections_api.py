from src.model import VideoCollection, VideoCollectionItem, VideoItem


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_add_collection_video_is_idempotent_and_removable_by_video_id(
    client, account_user, test_db
):
    collection = VideoCollection.create(name="接口移出合集", description="")
    video = VideoItem.create(title="接口移出视频")
    headers = _auth_headers(client, account_user)

    add_response = client.put(
        f"/video-collections/{collection.id}/videos/{video.id}",
        headers=headers,
    )
    assert add_response.status_code == 204
    assert VideoCollectionItem.select().count() == 1

    # 幂等：重复加入仍 204，不产生重复成员。
    again_add = client.put(
        f"/video-collections/{collection.id}/videos/{video.id}",
        headers=headers,
    )
    assert again_add.status_code == 204
    assert VideoCollectionItem.select().count() == 1

    remove_response = client.delete(
        f"/video-collections/{collection.id}/videos/{video.id}",
        headers=headers,
    )
    assert remove_response.status_code == 204
    assert VideoCollectionItem.select().count() == 0

    # 幂等：重复移出（或对非成员调用）仍返回 204。
    again_response = client.delete(
        f"/video-collections/{collection.id}/videos/{video.id}",
        headers=headers,
    )
    assert again_response.status_code == 204


def test_remove_collection_video_rejects_unknown_collection(
    client, account_user, test_db
):
    video = VideoItem.create(title="未知合集视频")
    headers = _auth_headers(client, account_user)

    response = client.delete(
        f"/video-collections/999999/videos/{video.id}",
        headers=headers,
    )

    assert response.status_code == 404


def test_list_collection_videos_exposes_position_and_video_key(
    client, account_user, test_db
):
    collection = VideoCollection.create(name="接口成员列表", description="")
    first = VideoItem.create(title="第一集")
    second = VideoItem.create(title="第二集")
    headers = _auth_headers(client, account_user)
    for video in (first, second):
        assert (
            client.put(
                f"/video-collections/{collection.id}/videos/{video.id}",
                headers=headers,
            ).status_code
            == 204
        )

    response = client.get(
        f"/video-collections/{collection.id}/videos", headers=headers
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["video"]["id"] for item in items] == [first.id, second.id]
    assert [item["position"] for item in items] == [0, 1]
    # 成员行 id 不再暴露：成员键就是 video.id。
    assert all("item_id" not in item for item in items)


def test_set_collection_videos_covers_reorder_and_batch_set(
    client, account_user, test_db
):
    collection = VideoCollection.create(name="接口整体设置", description="")
    first = VideoItem.create(title="第一集")
    second = VideoItem.create(title="第二集")
    headers = _auth_headers(client, account_user)

    response = client.put(
        f"/video-collections/{collection.id}/videos",
        json={"video_item_ids": [second.id, first.id, second.id]},
        headers=headers,
    )

    assert response.status_code == 204
    links = list(
        VideoCollectionItem.select().order_by(VideoCollectionItem.position)
    )
    # 去重保序；整体设置同时覆盖加入、移出与重排。
    assert [link.video_item_id for link in links] == [second.id, first.id]
    assert [link.position for link in links] == [0, 1]

    clear_and_add = client.put(
        f"/video-collections/{collection.id}/videos",
        json={"video_item_ids": [first.id]},
        headers=headers,
    )
    assert clear_and_add.status_code == 204
    assert [
        link.video_item_id
        for link in VideoCollectionItem.select().order_by(VideoCollectionItem.position)
    ] == [first.id]


def test_set_collection_videos_rejects_unknown_video(client, account_user, test_db):
    collection = VideoCollection.create(name="接口整体设置校验", description="")
    headers = _auth_headers(client, account_user)

    response = client.put(
        f"/video-collections/{collection.id}/videos",
        json={"video_item_ids": [999999]},
        headers=headers,
    )

    assert response.status_code == 404
