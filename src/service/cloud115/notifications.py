from src.model import SystemNotification
from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService


def create_cloud115_cookies_expired_notification(
    *, library_name: str, library_id: int
) -> NotificationResource | None:
    """同一媒体库只保留一条未读的 cookies 失效通知。"""
    title = f"115 网盘登录已失效（{library_name}）"
    already_pending = (
        SystemNotification.select()
        .where(
            SystemNotification.related_resource_type == "media_library",
            SystemNotification.related_resource_id == library_id,
            SystemNotification.category == "warning",
            SystemNotification.title.startswith("115 网盘登录已失效"),
            SystemNotification.is_read == False,
        )
        .exists()
    )
    if already_pending:
        return None
    return NotificationService.create(
        NotificationDraft(
            category="warning",
            title=title,
            content=(
                "115 网盘 cookies 已失效，该媒体库的播放、缩略图与导入能力已不可用，"
                "请在媒体库设置中重新扫码登录。"
            ),
            related_resource_type="media_library",
            related_resource_id=library_id,
        )
    )
