"""下载任务列表接口按 provider 协议状态筛选的回归测试。"""

from src.model import DownloadClient, DownloadTask, Image, MediaLibrary, Movie


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_tasks():
    library = MediaLibrary.create(name="lib", provider_key="test", provider_config={})
    download_client = DownloadClient.create(
        name="client",
        library=library,
        provider_config={},
    )
    for index, (state, movie) in enumerate(
        [("downloading", "ABP-001"), ("failed", "ABP-002"), ("completed", "ABP-003")]
    ):
        DownloadTask.create(
            client=download_client,
            movie=movie,
            name=f"{movie}.mkv",
            remote_id=f"remote-{index}",
            progress=1.0 if state == "completed" else 0.5,
            state=state,
            completed_source_ref={"source": movie} if state == "completed" else None,
            import_status="pending",
        )
    return download_client


def test_download_tasks_filters_by_multi_state_query(client, account_user):
    token = _login(client, account_user.username)
    _seed_tasks()

    response = client.get(
        "/download-tasks",
        params=[
            ("page", "1"),
            ("page_size", "20"),
            ("state", "downloading"),
            ("state", "failed"),
        ],
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {item["state"] for item in body["items"]} == {
        "downloading",
        "failed",
    }


def test_download_tasks_filters_by_single_state_query(client, account_user):
    token = _login(client, account_user.username)
    _seed_tasks()

    response = client.get(
        "/download-tasks",
        params={"page": 1, "page_size": 20, "state": "completed"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["state"] == "completed"


def test_download_tasks_ignores_body_as_filter(client, account_user):
    """状态筛选只接受 query，不从 GET body 读取。"""
    token = _login(client, account_user.username)
    _seed_tasks()

    response = client.request(
        "GET",
        "/download-tasks?page=1&page_size=20",
        headers={**_auth(token), "Content-Type": "application/json"},
        content='["completed"]',
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 3


def test_download_tasks_returns_movie_cover_variants(client, account_user):
    token = _login(client, account_user.username)
    _seed_tasks()
    cover = Image.create(
        origin="/files/images/cover.jpg",
        small="/files/images/cover-small.jpg",
        medium="/files/images/cover-medium.jpg",
        large="/files/images/cover-large.jpg",
    )
    thin_cover = Image.create(
        origin="/files/images/thin-cover.jpg",
        small="/files/images/thin-cover-small.jpg",
        medium="/files/images/thin-cover-medium.jpg",
        large="/files/images/thin-cover-large.jpg",
    )
    Movie.create(
        movie_number="ABP-001",
        javdb_id="download-task-abp-001",
        title="下载任务影片",
        cover_image=cover,
        thin_cover_image=thin_cover,
    )

    response = client.get(
        "/download-tasks",
        params={"movie_number": "ABP-001"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["movie_title"] == "下载任务影片"
    assert item["movie_cover"]["large"] == "/files/images/cover-large.jpg"
    assert item["movie_thin_cover"]["large"] == "/files/images/thin-cover-large.jpg"
