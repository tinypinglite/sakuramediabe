from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService

AUTH_EXPIRED_EVENT_TYPE = "cloud115_auth_expired"


def _auth_expired_dedupe_key(library_id: int) -> str:
    return f"{AUTH_EXPIRED_EVENT_TYPE}:media_library:{library_id}"


def release_cloud115_auth_expired_notification(library_id: int) -> int:
    """重新授权成功后释放旧失效事件，使下一次真实失效可再次提醒。"""
    return NotificationService.release_notification_dedupe_key(
        _auth_expired_dedupe_key(library_id)
    )


def create_cloud115_cookies_expired_notification(
    *, library_name: str, library_id: int
) -> NotificationResource:
    """同一媒体库的 cookies 失效事件只创建一次通知。"""
    title = f"115 网盘登录已失效（{library_name}）"
    return NotificationService.create_once(
        NotificationDraft(
            category="warning",
            title=title,
            content=(
                "115 网盘 cookies 已失效，该媒体库的播放、缩略图与导入能力已不可用，"
                "请在媒体库设置中重新扫码登录。"
            ),
            event_type=AUTH_EXPIRED_EVENT_TYPE,
            dedupe_key=_auth_expired_dedupe_key(library_id),
            resource_type="media_library",
            resource_id=library_id,
            related_resource_type="media_library",
            related_resource_id=library_id,
        )
    )
