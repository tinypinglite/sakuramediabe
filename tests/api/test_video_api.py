def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_video_item_create_and_filter_by_query(client, account_user):
    token = _login(client, username=account_user.username)
    headers = _headers(token)

    created = client.post(
        "/videos",
        json={
            "title": "海边录像",
            "summary": "测试视频",
        },
        headers=headers,
    )
    assert created.status_code == 201
    detail = created.json()
    assert detail["title"] == "海边录像"
    assert "tags" not in detail
    assert "persons" not in detail
    assert detail["media_count"] == 0
    assert detail["can_play"] is False

    client.post("/videos", json={"title": "森林散步"}, headers=headers)

    # 按标题关键字过滤能命中。
    listed = client.get("/videos?query=海边", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == detail["id"]


def test_video_item_update_fields(client, account_user):
    token = _login(client, username=account_user.username)
    headers = _headers(token)

    video = client.post(
        "/videos",
        json={"title": "片子"},
        headers=headers,
    ).json()

    updated = client.patch(
        f"/videos/{video['id']}",
        json={"title": "新标题", "summary": "新简介"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "新标题"
    assert updated.json()["summary"] == "新简介"


def test_video_collection_ordering_and_reorder(client, account_user):
    token = _login(client, username=account_user.username)
    headers = _headers(token)

    collection = client.post(
        "/video-collections",
        json={"name": "我的合集"},
        headers=headers,
    ).json()

    video_ids = []
    for index in range(3):
        video = client.post("/videos", json={"title": f"片{index}"}, headers=headers).json()
        video_ids.append(video["id"])
        add = client.post(
            f"/video-collections/{collection['id']}/items",
            json={"video_item_id": video["id"]},
            headers=headers,
        )
        assert add.status_code == 204

    items = client.get(f"/video-collections/{collection['id']}/items", headers=headers).json()
    # 默认按加入顺序（position 递增）。
    assert [item["video"]["id"] for item in items] == video_ids
    assert [item["position"] for item in items] == [0, 1, 2]

    # 合集 item_count 同步。
    assert client.get(f"/video-collections/{collection['id']}", headers=headers).json()["item_count"] == 3

    # 倒序重排。
    reordered_item_ids = [items[2]["item_id"], items[1]["item_id"], items[0]["item_id"]]
    reorder = client.post(
        f"/video-collections/{collection['id']}/items/reorder",
        json={"ordered_item_ids": reordered_item_ids},
        headers=headers,
    )
    assert reorder.status_code == 200
    assert [item["video"]["id"] for item in reorder.json()] == [video_ids[2], video_ids[1], video_ids[0]]
    assert [item["position"] for item in reorder.json()] == [0, 1, 2]


def test_video_collection_reorder_rejects_incomplete_item_set(client, account_user):
    token = _login(client, username=account_user.username)
    headers = _headers(token)

    collection = client.post("/video-collections", json={"name": "C"}, headers=headers).json()
    video = client.post("/videos", json={"title": "x"}, headers=headers).json()
    client.post(
        f"/video-collections/{collection['id']}/items",
        json={"video_item_id": video["id"]},
        headers=headers,
    )
    items = client.get(f"/video-collections/{collection['id']}/items", headers=headers).json()

    # 漏排（给出空列表中缺成员）应被拒绝。
    response = client.post(
        f"/video-collections/{collection['id']}/items/reorder",
        json={"ordered_item_ids": [items[0]["item_id"] + 999]},
        headers=headers,
    )
    assert response.status_code == 422


def test_videos_routes_require_authentication(client):
    assert client.get("/videos").status_code == 401
    assert client.get("/video-collections").status_code == 401
