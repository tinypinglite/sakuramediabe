from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService


def create_cloud115_offline_abandoned_notification(
    *, task_name: str, task_id: int
) -> NotificationResource:
    return NotificationService.create_once(
        NotificationDraft(
            category="warning",
            title=f"115 离线下载超时已放弃（{task_name}）",
            content=(
                "该 115 离线任务提交后长时间未完成（通常是磁力无种或冷门资源），"
                "系统已停止跟踪其进度。115 网盘中的离线任务仍保留，可自行到 115 处理。"
                "后续自动下载任务可能会为该影片重新搜索其它资源，也可删除本任务后手动重试。"
            ),
            event_type="cloud115_offline_abandoned",
            dedupe_key=f"cloud115_offline_abandoned:download_task:{task_id}",
            resource_type="download_task",
            resource_id=task_id,
            related_resource_type="download_task",
            related_resource_id=task_id,
        )
    )
