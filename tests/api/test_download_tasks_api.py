"""下载任务列表接口按状态筛选的回归测试。

历史事故：router 用 `DownloadTasksQuery = Depends()` 承接 query，其中
`download_state: list[str]` 在 FastAPI 0.110 下会被当作 body 参数解析，
URL 里的 `?download_state=...` 被静默忽略，前端状态筛选一直失效。
修复后 download_state 显式走 Query，本文件钉住 query 多值筛选行为。
"""

from src.model import DownloadClient, DownloadTask, MediaLibrary


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_tasks():
    library = MediaLibrary.create(name="lib", backend="local")
    download_client = DownloadClient.create(
        name="qb",
        kind="qbittorrent",
        media_library=library,
    )
    for index, (state, movie) in enumerate(
        [("downloading", "ABP-001"), ("stalled", "ABP-002"), ("seeding", "ABP-003")]
    ):
        DownloadTask.create(
            client=download_client,
            movie=movie,
            name=f"{movie}.mkv",
            info_hash=f"hash-{index}",
            save_path="/downloads",
            progress=1.0 if state == "seeding" else 0.5,
            download_state=state,
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
            ("download_state", "downloading"),
            ("download_state", "stalled"),
        ],
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {item["download_state"] for item in body["items"]} == {
        "downloading",
        "stalled",
    }


def test_download_tasks_filters_by_single_state_query(client, account_user):
    token = _login(client, account_user.username)
    _seed_tasks()

    response = client.get(
        "/download-tasks",
        params={"page": 1, "page_size": 20, "download_state": "seeding"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["download_state"] == "seeding"


def test_download_tasks_ignores_body_as_filter(client, account_user):
    """回归：修复前 list 字段被 FastAPI 当 body 参数，GET body 裸数组反而生效。"""
    token = _login(client, account_user.username)
    _seed_tasks()

    response = client.request(
        "GET",
        "/download-tasks?page=1&page_size=20",
        headers={**_auth(token), "Content-Type": "application/json"},
        content='["downloading"]',
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 3


def test_download_tasks_returns_snapshot_defaults_and_polling_updates(client, account_user):
    token = _login(client, account_user.username)
    download_client = _seed_tasks()

    first = client.get(
        "/download-tasks",
        params={"client_id": download_client.id, "movie_number": "ABP-001"},
        headers=_auth(token),
    )

    assert first.status_code == 200, first.text
    old_item = first.json()["items"][0]
    assert old_item["raw_state"] == ""
    assert old_item["download_speed_bytes"] == 0
    assert old_item["uploaded_speed_bytes"] == 0
    assert old_item["downloaded_bytes"] == 0
    assert old_item["total_size_bytes"] == 0
    assert old_item["eta_seconds"] is None
    assert old_item["progress_synced_at"] is None

    DownloadTask.update(
        raw_state="downloading",
        download_speed_bytes=2_048,
        uploaded_speed_bytes=128,
        downloaded_bytes=1_024,
        total_size_bytes=4_096,
        eta_seconds=90,
        progress_synced_at="2026-08-20 12:00:00",
    ).where(DownloadTask.info_hash == "hash-0").execute()

    second = client.get(
        "/download-tasks",
        params={"client_id": download_client.id, "movie_number": "ABP-001"},
        headers=_auth(token),
    )

    assert second.status_code == 200, second.text
    item = second.json()["items"][0]
    assert item["raw_state"] == "downloading"
    assert item["download_speed_bytes"] == 2_048
    assert item["uploaded_speed_bytes"] == 128
    assert item["downloaded_bytes"] == 1_024
    assert item["total_size_bytes"] == 4_096
    assert item["eta_seconds"] == 90
    assert item["progress_synced_at"] == "2026-08-20T12:00:00"
