from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService


def create_ranking_account_error_notification(
    *, related_task_run_id: int | None = None
) -> NotificationResource:
    return NotificationService.create(
        NotificationDraft(
            category="warning",
            title="JavDB 账号登录失败",
            content=(
                "无法登录 JavDB 账号，已跳过需登录的 TOP250 榜单同步，请检查 "
                "config.toml 中的 javdb_username / javdb_password。"
            ),
            related_task_run_id=related_task_run_id,
        )
    )
