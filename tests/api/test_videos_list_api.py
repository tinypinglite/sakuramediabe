from src.model import VideoCollection, VideoCollectionItem, VideoItem


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_video_list_uncollected_excludes_collection_members(
    client, account_user, test_db
):
    member = VideoItem.create(title="已归类视频")
    outsider = VideoItem.create(title="未归类视频")
    collection = VideoCollection.create(name="视频列表未归类过滤合集", description="")
    VideoCollectionItem.create(collection=collection, video_item=member, position=0)

    response = client.get(
        "/videos",
        params={"uncollected": True},
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [outsider.id]
    assert body["total"] == 1


def test_video_list_default_keeps_collection_members(client, account_user, test_db):
    member = VideoItem.create(title="默认保留的视频")
    collection = VideoCollection.create(name="视频列表默认显示合集", description="")
    VideoCollectionItem.create(collection=collection, video_item=member, position=0)

    response = client.get("/videos", headers=_auth_headers(client, account_user))

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [member.id]
    assert body["total"] == 1
