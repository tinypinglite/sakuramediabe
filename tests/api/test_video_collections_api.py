from src.model import VideoCollection, VideoCollectionItem, VideoItem


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_remove_collection_video_by_video_id_is_idempotent(
    client, account_user, test_db
):
    collection = VideoCollection.create(name="接口移出合集", description="")
    video = VideoItem.create(title="接口移出视频")
    headers = _auth_headers(client, account_user)

    add_response = client.post(
        f"/video-collections/{collection.id}/items",
        json={"video_item_id": video.id},
        headers=headers,
    )
    assert add_response.status_code == 204
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
