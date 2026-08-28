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

import pytest
from peewee import IntegrityError

from src.model import SystemNotification
from src.service.system.activity import NotificationDraft, NotificationService


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


def test_activity_bootstrap_does_not_expose_event_cursor(client, account_user):
    token = _login(client, account_user.username)
    _create_notification("info", "bootstrap-notification")

    response = client.get("/system/activity/bootstrap", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    assert "latest_event_id" not in body
    assert body["unread_count"] == 1
    assert body["notifications"]["total"] == 1


def test_notification_create_once_returns_existing_record_and_creates_one_row(test_db):
    draft = NotificationDraft(
        category="warning",
        title="Provider 登录已失效",
        content="请重新登录。",
        event_type="provider_auth_expired",
        dedupe_key="provider_auth_expired:media_library:1",
        resource_type="media_library",
        resource_id=1,
    )

    first = NotificationService.create_once(draft)
    second = NotificationService.create_once(draft)

    assert second.id == first.id
    assert first.event_type == "provider_auth_expired"
    assert first.dedupe_key == "provider_auth_expired:media_library:1"
    assert first.resource_type == "media_library"
    assert first.resource_id == 1
    assert SystemNotification.select().where(
        SystemNotification.dedupe_key == draft.dedupe_key
    ).count() == 1


def test_notification_model_rejects_duplicate_non_null_dedupe_key(test_db):
    SystemNotification.create(
        category="info",
        title="first",
        content="first",
        dedupe_key="notification:test-duplicate",
    )

    with pytest.raises(IntegrityError):
        SystemNotification.create(
            category="info",
            title="second",
            content="second",
            dedupe_key="notification:test-duplicate",
        )
