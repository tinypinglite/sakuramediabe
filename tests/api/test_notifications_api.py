"""通知列表/已读接口的回归测试。

历史事故：`ActivityService` 门面把 `TaskRunService` 和 `NotificationService` 平铺
多继承，两者的 `to_resource` / `build_query` / `page_query` 同名，MRO 里
`TaskRunService` 更靠前。`NotificationService.list_notifications` 内部写的是
`cls.build_query(category=...)`，经门面调用时 `cls` 是 `ActivityService`，于是打到
`TaskRunService.build_query(state=..., task_key=...)` → TypeError → 500。

首屏不受影响（bootstrap 走显式类名调用），只有前端改分类筛选 / 顶栏刷新才会打
`GET /system/notifications`，所以线上表现为「进页面正常、一动筛选就失败」。

根因已修：两侧方法改名为 `*_notification_*` / `*_task_run_*`，冲突集为空。结构性
不变式由 `tests/test_architecture_boundaries.py` 的多继承 facade 守卫钉住，本文件
守的是接口行为本身。
"""

from src.model import SystemNotification


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_notification(category: str, title: str, *, is_read: bool = False):
    return SystemNotification.create(
        category=category,
        title=title,
        content=f"content-{title}",
        is_read=is_read,
    )


def test_list_notifications_returns_all_without_filter(client, account_user):
    token = _login(client, account_user.username)
    _create_notification("info", "n-info")
    _create_notification("reminder", "n-reminder")

    response = client.get("/system/notifications", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["title"] for item in body["items"]} == {"n-info", "n-reminder"}


def test_list_notifications_filters_by_category(client, account_user):
    """直接复现事故：带 category 的请求以前会 500。"""
    token = _login(client, account_user.username)
    _create_notification("info", "n-info")
    _create_notification("reminder", "n-reminder")
    _create_notification("reminder", "n-reminder-2")

    response = client.get(
        "/system/notifications",
        params={"page": 1, "page_size": 20, "category": "reminder"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {item["title"] for item in body["items"]} == {"n-reminder", "n-reminder-2"}
    assert {item["category"] for item in body["items"]} == {"reminder"}


def test_list_notifications_filters_by_is_read(client, account_user):
    token = _login(client, account_user.username)
    _create_notification("info", "n-unread")
    _create_notification("info", "n-read", is_read=True)

    response = client.get(
        "/system/notifications",
        params={"is_read": False},
        headers=_auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["title"] for item in body["items"]] == ["n-unread"]


def test_list_notifications_rejects_unknown_category(client, account_user):
    token = _login(client, account_user.username)

    response = client.get(
        "/system/notifications",
        params={"category": "not-a-category"},
        headers=_auth(token),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_activity_filter"


def test_mark_single_notification_read_returns_resource(client, account_user):
    """同一个 MRO 劫持也埋在 mark_notification_read 的 cls.to_resource 上。"""
    token = _login(client, account_user.username)
    notification = _create_notification("warning", "n-warning")

    response = client.patch(
        f"/system/notifications/{notification.id}/read",
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == notification.id
    assert body["is_read"] is True
    assert SystemNotification.get_by_id(notification.id).is_read is True
